-- Attachments: a file that is not a document.
--
-- An image, a PDF, a spreadsheet. Bytes with a path, an owner and a media
-- type, which a page can point at and a reader can fetch.
--
------------------------------------------------------------------------------
-- Why the bytes are here and the identity is not
------------------------------------------------------------------------------
--
-- An attachment **is** a `wiki.document` row. Not "has one", not "looks like
-- one" -- it is one, and that is the whole design.
--
-- The alternative is a second table with a `path ltree` of its own, and it
-- fails on two counts. The ACL is keyed on `document.path`, so a separate tree
-- would need the ACL applied to it a second time, and a second copy of a
-- permission model is the copy that drifts. And `document_path_key` is a
-- unique constraint on one table: two tables holding paths could each hold
-- `root.public.logo`, which is a page and a file at one address, and no route
-- or mount can answer that.
--
-- Being a document row buys, for free and without a line of new ACL code:
-- inheritance from the containing folder, per-attachment ACEs, ownership,
-- `inheritance_blocked`, traversal, one unique path space, and the audit
-- trail. What is left over is the part that is genuinely different -- the
-- bytes -- and it lives here.
--
-- `is_attachment` on the document row is what tells a client which of the
-- three kinds it is holding, without joining to this table. That matters:
-- `current_document` is read by every request, and a join would put this
-- table's row-level policy on the hot path -- one ACL walk per attachment per
-- manifest. The flag is maintained by a trigger on this table rather than by
-- whoever writes it, so it cannot drift; 110_attachment_test.sql asserts the
-- equivalence over every row.
--
------------------------------------------------------------------------------
-- No history, on purpose
------------------------------------------------------------------------------
--
-- A page keeps every revision because text is small and a diff is meaningful.
-- Neither is true here. `document_version`'s own comment already warns that
-- full snapshots stop being free when the content is not markdown, and a
-- five-megabyte image kept once per edit is exactly the case it warns about.
--
-- So replacing an attachment overwrites it, and removing one is a delete
-- rather than a tombstone. That makes `delete` and `purge` the same act for an
-- attachment, and the policy in 050_rls.sql asks for the stronger of the two.

------------------------------------------------------------------------------
-- Settings, of which there is currently one
------------------------------------------------------------------------------
--
-- The size cap has to be enforced by the database, because the database is the
-- only thing every writer goes through -- the CLI, a hand-rolled client, psql.
-- A CHECK constraint cannot read a limit that an operator can change, so the
-- limit is a row and a trigger reads it.
--
-- A row rather than a GUC, and that is a security decision rather than taste.
-- `current_setting('fswiki.max_attachment_bytes')` would be read from the
-- *session*, and any role may SET a custom GUC in its own session -- so a
-- client could raise its own limit. This table is granted to no client role at
-- all; only the definer function in runtime/085 reads it.
create table wiki.setting (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

comment on table wiki.setting is
  'Operator settings the database itself enforces. Granted to no client role: '
  'a limit a caller can read is fine, a limit a caller can move is not.';

------------------------------------------------------------------------------
-- The bytes
------------------------------------------------------------------------------

create table wiki.attachment (
  -- Primary key *and* foreign key. One attachment per document, and no
  -- attachment without one -- so a row here can never be an orphan with no
  -- path, no owner and therefore no ACL.
  document_id uuid primary key references wiki.document(id) on delete cascade,

  bytes       bytea not null,
  -- The type as declared by whoever uploaded it. Constrained rather than free
  -- text because it is a hint the server may repeat in a `Content-Type`
  -- header: RFC 6838 restricted-name characters only, one slash, no
  -- parameters, and therefore no semicolon, no space and no newline. A media
  -- type cannot be used to inject a header, and the server narrows it further
  -- before sending -- see fswiki_core.pages.INLINE.
  media_type  text not null,

  -- Both generated, so neither can disagree with the bytes. `byte_size` is
  -- what a client needs to stat a file without fetching it, exactly as
  -- `octet_length(content)` is for a page.
  byte_size   integer generated always as (octet_length(bytes)) stored,
  sha256      bytea generated always as (digest(bytes, 'sha256')) stored,

  created_at  timestamptz not null default now(),
  created_by  uuid references wiki.principal(id) on delete set null,
  updated_at  timestamptz not null default now(),
  updated_by  uuid references wiki.principal(id) on delete set null,

  -- Zero bytes is a file, and refusing it here would be this schema having an
  -- opinion about content. The upper bound is a trigger, because it is
  -- configurable; see wiki.max_attachment_bytes() in runtime/085.
  constraint attachment_media_type_shape check (
    media_type ~ '^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$')
);

comment on table wiki.attachment is
  'The bytes of an attachment. Identity, path, owner and ACL are the '
  'wiki.document row this points at; only the content is here.';

comment on column wiki.attachment.bytes is
  'Storage is EXTENDED, the default: out of line when large, compressed if it '
  'helps. Left alone deliberately -- a PNG will not compress and pays a little '
  'CPU for the attempt, while a CSV compresses well, and guessing wrong for '
  'one of them is cheaper than a knob nobody will tune.';

-- Finding an attachment by its content, for dedupe and for "where else is
-- this file". Not unique: the same bytes at two paths are two attachments,
-- with two ACLs.
create index attachment_sha256_idx on wiki.attachment (sha256);

------------------------------------------------------------------------------
-- Which kind of thing a document row is
------------------------------------------------------------------------------
--
-- Three kinds now: a folder, a page, an attachment. `is_folder` was enough for
-- two. The pair is constrained rather than replaced by an enum because an enum
-- is a migration for every reader of `is_folder`, and there is no third
-- combination to represent: a folder holds children, an attachment holds
-- bytes, and nothing does both.
alter table wiki.document
  add column is_attachment boolean not null default false;

alter table wiki.document
  add constraint document_kind_shape check (not (is_folder and is_attachment));

comment on column wiki.document.is_attachment is
  'True when wiki.attachment holds this document''s bytes. Maintained by a '
  'trigger on that table, so it cannot be set to a lie.';

-- The mount and the browser both ask "what is under here"; only the browser
-- can serve bytes. See syncable_document in runtime/070_views.sql.
create index document_attachment_idx on wiki.document (id) where is_attachment;

------------------------------------------------------------------------------
-- Row-level security
------------------------------------------------------------------------------
-- The policies are in runtime/050_rls.sql with the rest. Enabling RLS is
-- state, so it is here: a table with policies and RLS switched off is a table
-- with no policies, and the two halves must never be separated by a restart.

alter table wiki.attachment enable row level security;
alter table wiki.setting    enable row level security;
