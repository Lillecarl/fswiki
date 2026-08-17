-- Dev-only seed, layered on top of server/test/010_fixtures.sql.
--
-- The fixtures build the interesting ACL shapes but give every page the same
-- one-line body, which makes a mounted filesystem dull to poke at. This adds
-- real markdown and a slightly deeper tree, and it does so by *publishing*
-- revisions rather than rewriting them — the document_version_immutable trigger
-- binds the table owner too, so there is no shortcut here even as postgres.
--
-- Loaded only by `fswiki-dev`. The test suite never sees it.

begin;

-- Publish a new revision the way wiki.push() would: close the live interval,
-- open the next. Same transaction, so the exclusion constraint is satisfied at
-- commit and there is never a moment with two live revisions.
create or replace function pg_temp.publish(p_path text, p_content text, p_message text default 'dev seed')
returns void
language plpgsql as $$
declare
  v_doc   uuid;
  v_next  integer;
  v_prev  uuid;
  v_close timestamptz;
begin
  select d.id into v_doc from wiki.document d where d.path = p_path::ltree;
  if v_doc is null then
    raise exception 'no such document: %', p_path;
  end if;

  -- `now()` is the transaction timestamp, so closing a revision that was opened
  -- earlier in *this* transaction would produce an empty range — which
  -- normalises to `empty`, whose lower() is null, which the immutability
  -- trigger reads as a rewrite. Step past the start instead.
  update wiki.document_version
     set valid = tstzrange(lower(valid),
                           greatest(now(), lower(valid) + interval '1 microsecond'))
   where document_id = v_doc and upper_inf(valid)
  returning id, version + 1, upper(valid) into v_prev, v_next, v_close;

  -- The successor opens exactly where its predecessor closed. Defaulting to
  -- now() would overlap whenever the predecessor was written in this same
  -- transaction, since now() is the transaction timestamp and does not advance.
  insert into wiki.document_version
    (document_id, version, path, content, message, author_id, parent_version_id, valid)
  select v_doc, coalesce(v_next, 1), p_path::ltree, p_content, p_message,
         (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice'),
         v_prev, tstzrange(coalesce(v_close, now()), null);
end;
$$;

create or replace function pg_temp.mkdoc(
  p_parent text, p_slug text, p_folder boolean, p_title text, p_owner text default 'alice'
) returns void language sql as $$
  insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
  select d.id, p_slug, p_folder, p_title,
         (select p.id from wiki.principal p where p.kind = 'user' and p.name = p_owner)
    from wiki.document d where d.path = p_parent::ltree;
$$;

------------------------------------------------------------------------------
-- Real content for the fixture pages
------------------------------------------------------------------------------

select pg_temp.publish('root.public.welcome', E'# Welcome\n\nThis is the fswiki development wiki. Everything under `public/` is\nreadable by anyone in the `everyone` group.\n\n- [Archive](archive/) holds retired material\n- [Guide](guide/) is the handbook\n\nEdit this file with your editor; the change lands in your drafts, not on the\nserver, until you push it.\n');

select pg_temp.publish('root.public.archive.old-post', E'# An Old Post\n\nKept for the record. Nobody has touched this since the archive was created.\n');

select pg_temp.publish('root.engineering.onboarding', E'# Onboarding\n\nCarol can read this page and nothing else under `engineering/` — an explicit\nallow on this document outranks the inherited `contractors` deny. That is the\nwhole point of per-object ACLs.\n\n## First week\n\n1. Get a token\n2. Mount the wiki\n3. Read the handbook\n');

select pg_temp.publish('root.engineering.secret-plans', E'# Secret Plans\n\nReadable in the browser, never on a laptop: an ACE denies the `sync` role to\n`everyone`, so this document is absent from `wiki.syncable_document` and will\nnot appear in a FUSE mount.\n\nIf you can see this file in your mount, the sync gate is broken.\n');

select pg_temp.publish('root.engineering.private.memo', E'# Memo\n\n`private/` blocks inheritance, so the engineering-wide editor grant stops at\nits boundary. Only this folder''s own ACE applies.\n');

select pg_temp.publish('root.locked', E'# Locked\n\nDave owns this and is denied everything on it, yet keeps `grant` — so he can\ndig himself out. Try `explain_acl` on it.\n');

select pg_temp.publish('root.io-test.child', E'# Child\n\nThe `reader` ACE on `io-test` is inherit-only, so it does not apply to the\nfolder itself but does apply here.\n');

------------------------------------------------------------------------------
-- A deeper subtree, so readdir has something to chew on
------------------------------------------------------------------------------

select pg_temp.mkdoc('root.public', 'guide', true, 'Handbook');

select pg_temp.mkdoc('root.public.guide', 'index',       false, 'Handbook');
select pg_temp.mkdoc('root.public.guide', 'mounting',    false, 'Mounting the wiki');
select pg_temp.mkdoc('root.public.guide', 'permissions', false, 'How permissions work');
select pg_temp.mkdoc('root.public.guide', 'pushing',     false, 'Drafts and pushing');

insert into wiki.document_version (document_id, version, path, content, message, author_id)
select d.id, 1, d.path, '# ' || d.title || E'\n\nTODO\n', 'stub',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d
 where d.path <@ 'root.public.guide'::ltree and not d.is_folder;

select pg_temp.publish('root.public.guide.index', E'# Handbook\n\n- [Mounting the wiki](mounting)\n- [How permissions work](permissions)\n- [Drafts and pushing](pushing)\n');

select pg_temp.publish('root.public.guide.mounting', E'# Mounting the wiki\n\n    fswiki-dev up &\n    export FSWIKI_TOKEN=$(fswiki-dev token bob)\n    fswiki-mount ~/wiki\n\nWhat you see is `wiki.syncable_document`, which is a subset of what you can\nread: the `sync` capability gates taking a local copy.\n');

select pg_temp.publish('root.public.guide.permissions', E'# How permissions work\n\nEvery document carries an ACL of allow/deny entries, each naming one principal\nand one role. Entries inherit down the tree with the four NTFS flags, and the\nnearest entry wins — deny before allow at equal distance.\n\nCapabilities form a lattice, so there is no write without read:\n\n    read <-- sync\n         <-- write\n         <-- create\n         <-- delete ------- purge\n         <-- grant <-- administer\n');

select pg_temp.publish('root.public.guide.pushing', E'# Drafts and pushing\n\nWriting through the mount does not publish. It records a draft, visible only to\nyou, composited over the published tree so your edits appear in place.\n\n    fswiki push -m "fix the handbook"\n\nPush is all-or-nothing. If anyone has moved past the revision you based an edit\non, the whole changeset is refused and you get the server''s copy back to merge\nagainst.\n');

-- A page nobody has ever published: a folder-shaped hole, to check that the
-- client copes with a document row whose version list is empty.
select pg_temp.mkdoc('root.public', 'unpublished', false, 'Never Published');

commit;
