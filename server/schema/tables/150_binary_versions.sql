-- A revision can hold bytes, and it can hold them somewhere else.
--
-- This supersedes 140_attachments.sql, which is four files back. That is a
-- short life for a table, and the reasoning is worth keeping rather than
-- quietly replacing.
--
------------------------------------------------------------------------------
-- Why the separate table was the wrong shape
------------------------------------------------------------------------------
--
-- 140 got the identity right and the body wrong. An attachment **is** a
-- `wiki.document` row -- that argument holds and is why the ACL needed no
-- second implementation. What it got wrong was putting the bytes in a table of
-- their own, outside the temporal model.
--
-- Everything a wiki does with a body, this schema already does through
-- `document_version`: history, `document_as_of`, `content_hash` for the sync
-- diff, `size` for a stat, `base_version` for conflict detection, tombstones,
-- drafts, and `wiki.push`. A separate table means writing a second, worse copy
-- of each -- and the first place that bit was the mount, which needs every one
-- of them at once.
--
-- Two things fall out of folding it in, and both are improvements rather than
-- consolations:
--
--   `content_type` is already the media type. `image/png` is a content type
--   the same way `text/markdown` is, so the column that existed for pages was
--   the column an attachment needed.
--
--   `document.is_attachment` disappears. It existed so `current_document`
--   could name the kind without joining a table whose policy costs an ACL walk
--   per row -- 0.35 ms each, 138 ms on a 1,420-row manifest. The version join
--   is already there, so the answer is free and there is no denormalisation
--   left to drift.
--
-- The cost, stated plainly: every revision of a binary keeps its bytes.
-- `document_version`'s own header warns about exactly that. The cap in
-- `wiki.setting` bounds each one, and external storage below is what bounds
-- the total.
--
------------------------------------------------------------------------------
-- Where the bytes are is a property of the revision
------------------------------------------------------------------------------
--
-- Not of the file. That is deliberate and it is what makes this open-ended: a
-- wiki can keep this month's images in the database and last year's in a
-- bucket, and each revision knows which without anything else moving.
--
-- A lookup table rather than an enum, and the reason is the migration chain.
-- `alter type ... add value` cannot use the new value in the transaction that
-- adds it, so adding a backend to an enum is two migrations that must not be
-- run together. A row is idempotent, it can be added by seed/ instead of by a
-- migration at all, and an operator can read the list.

create table wiki.storage_backend (
  name        text primary key,
  description text not null
);

comment on table wiki.storage_backend is
  'Where a revision''s bytes can live. Adding one is a seed row plus a store '
  'implementation in fswiki_core.attachments; nothing in this file changes.';

insert into wiki.storage_backend (name, description) values
  ('database', 'In document_version.content or content_bytes, in this database.');

------------------------------------------------------------------------------
-- The columns
------------------------------------------------------------------------------

alter table wiki.document_version
  -- The body, when it is not text. Exactly one of `content` and
  -- `content_bytes` is set, and neither is when the bytes are elsewhere.
  add column content_bytes bytea,

  add column storage text not null default 'database'
    references wiki.storage_backend(name),

  -- Where to get it, when it is not here. The backend decides the shape --
  -- `s3://bucket/key`, a path, a URL. Opaque to this schema on purpose: a
  -- column that parsed it would be a second place to teach about a backend.
  add column locator text,

  -- Stored rather than computed, because for an external body there is nothing
  -- here to measure. A trigger fills it from the bytes when they are local,
  -- which is what keeps the two from disagreeing; see runtime/085.
  add column byte_size bigint;

comment on column wiki.document_version.content_bytes is
  'A binary body. Exactly one of content, content_bytes and locator says where '
  'the bytes are, and a tombstone says none of them.';

comment on column wiki.document_version.storage is
  'Which backend holds this revision''s bytes. Per revision, not per document: '
  'old images can move to a bucket while new ones stay here.';

-- One body, or none. `num_nonnulls` rather than three ORs, because the day a
-- fourth kind of body appears this reads the same.
alter table wiki.document_version
  add constraint document_version_one_body
    check (num_nonnulls(content, content_bytes) <= 1);

-- Local storage keeps no locator; external storage keeps no bytes. Stated in
-- both directions so neither half can be half-set.
alter table wiki.document_version
  add constraint document_version_storage_shape check (
    case when storage = 'database'
         then locator is null
         else locator is not null
              and content is null and content_bytes is null
    end);

-- A tombstone is the absence of a body, whichever kind it would have been.
-- 140's version of this check knew only about `content`.
alter table wiki.document_version
  drop constraint document_version_tombstone_empty;

alter table wiki.document_version
  add constraint document_version_tombstone_empty check (
    not is_tombstone
    or (content is null and content_bytes is null and locator is null));

-- A size for every revision that has a body, and none for a tombstone.
alter table wiki.document_version
  add constraint document_version_size_shape check (
    (byte_size is null) = (content is null and content_bytes is null
                           and locator is null));

------------------------------------------------------------------------------
-- content_hash stops being generated
------------------------------------------------------------------------------
--
-- It was `digest(coalesce(content, ''), 'sha256')`, which cannot see bytes and
-- cannot see a body that is not here at all. A generated column may only read
-- its own row's columns, and for external storage the answer has to come from
-- whoever uploaded it -- so this becomes an ordinary column that a trigger
-- fills and refuses to let a caller contradict.
--
-- The value does not change for existing rows. pgcrypto's `digest(text, ...)`
-- hashes the text in the database encoding, so on a UTF8 database it is
-- exactly `digest(convert_to(content, 'UTF8'), 'sha256')` -- and
-- 120_binary_test.sql asserts that rather than trusting this paragraph.
alter table wiki.document_version drop column content_hash;

alter table wiki.document_version add column content_hash bytea;

comment on column wiki.document_version.content_hash is
  'sha256 of the body, whatever kind it is. Filled by a trigger for a local '
  'body; supplied by the uploader for an external one, because there is '
  'nothing here to hash.';

------------------------------------------------------------------------------
-- Drafts carry bytes too
------------------------------------------------------------------------------
--
-- The whole claim of the mount is that everything you do there is a draft
-- until you push. A binary file that published itself on `cp` would be that
-- claim with an exception in it, and an exception nobody can see from the
-- filesystem.
--
-- No `storage` here. A draft is unpublished work in this database by
-- definition; where it ends up is decided when it is pushed.
alter table wiki.draft
  add column content_bytes bytea;

alter table wiki.draft
  add constraint draft_one_body
    check (num_nonnulls(content, content_bytes) <= 1);

-- `content is not null` in the old shape check meant "there is a body". It has
-- to mean the same thing now that a body can be bytes.
alter table wiki.draft drop constraint draft_shape;

alter table wiki.draft add constraint draft_shape check (
  case operation
    when 'create' then document_id is null and base_version is null
                       and num_nonnulls(content, content_bytes) = 1
    when 'update' then document_id is not null and base_version is not null
                       and num_nonnulls(content, content_bytes) = 1
    when 'delete' then document_id is not null and base_version is not null
    when 'move'   then document_id is not null and base_version is not null
  end);

------------------------------------------------------------------------------
-- Move what 140 stored, then take it away
------------------------------------------------------------------------------
--
-- Each attachment becomes revision 1 of the document it already was. The
-- document row is unchanged -- that half of 140 was right -- so the path, the
-- owner, the ACEs and the audit trail all survive untouched.

insert into wiki.document_version
  (document_id, version, path, content, content_bytes, content_type,
   byte_size, content_hash, storage, message, author_id, created_at, valid)
select a.document_id, 1, d.path, null, a.bytes, a.media_type,
       a.byte_size, a.sha256, 'database', 'attached',
       a.created_by, a.created_at, tstzrange(a.created_at, null)
  from wiki.attachment a
  join wiki.document d on d.id = a.document_id;

-- Everything that was generated has to be backfilled, because a trigger added
-- in runtime/ does not run over rows that are already here.
update wiki.document_version
   set content_hash = digest(coalesce(content_bytes,
                                      convert_to(coalesce(content, ''), 'UTF8')),
                             'sha256'),
       byte_size    = octet_length(coalesce(content_bytes,
                                            convert_to(content, 'UTF8')))
 where content_hash is null;

drop table wiki.attachment;

alter table wiki.document drop column is_attachment;

------------------------------------------------------------------------------
-- Finding the binaries
------------------------------------------------------------------------------
--
-- Small and partial: almost every revision is text, so this indexes the few
-- that are not. It is what "which files are in the bucket" will need.
create index document_version_binary_idx
  on wiki.document_version (document_id)
  where content_bytes is not null or storage <> 'database';
