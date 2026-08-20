-- Bodies that are not text: the limit, the bookkeeping, and the two verbs.
--
-- tables/150_binary_versions.sql has the design and why 140's separate table
-- went away. This file is the moving parts.

------------------------------------------------------------------------------
-- The configurable limit
------------------------------------------------------------------------------
--
-- SECURITY DEFINER because `wiki.setting` is granted to no client role. The
-- *value* is not a secret -- the error below names it, because a refusal a
-- person cannot act on is a bug -- but the row must not be writable by anyone
-- the wiki serves.
--
-- A row and not a GUC. `current_setting('fswiki.max_attachment_bytes')` reads
-- from the session, and any role may SET a custom GUC in its own session, so a
-- client could raise its own cap.
create or replace function wiki.max_attachment_bytes()
returns bigint
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select coalesce(
    (select nullif(value, '')::bigint from wiki.setting
      where key = 'max_attachment_bytes'),
    10485760);
$$;

comment on function wiki.max_attachment_bytes() is
  'The cap on a binary body, in bytes. From wiki.setting, which no client role '
  'may read or write; 10 MiB if the row is missing.';

------------------------------------------------------------------------------
-- A revision's size and hash follow its body
------------------------------------------------------------------------------
--
-- `content_hash` used to be a generated column and `byte_size` did not exist.
-- Neither can be generated now: a generated column reads only its own row, and
-- a revision whose bytes are in a bucket has nothing here to measure. So a
-- trigger fills both when the body is local, and *requires* both when it is
-- not -- which is the only honest arrangement, because the uploader is the
-- only party that saw the bytes.
--
-- It overwrites rather than validates for a local body. A caller who supplies
-- a hash that disagrees with the bytes is not making a claim worth keeping.
--
-- SECURITY DEFINER for the cap alone: wiki.setting is readable by nobody the
-- wiki serves, and the check has to happen on the table rather than in
-- wiki.attach(), because psql is a client too.
create or replace function wiki.version_fill_body()
returns trigger
language plpgsql security definer
set search_path = wiki, public, pg_temp as $$
declare
  body bytea;
  cap  bigint;
begin
  if new.is_tombstone then
    new.byte_size := null;
    new.content_hash := null;
    return new;
  end if;

  if new.storage <> 'database' then
    if new.byte_size is null or new.content_hash is null then
      raise exception
        'a revision stored in % must carry its own byte_size and content_hash',
        new.storage
        using errcode = '22023',
              hint = 'nothing in this database saw the bytes';
    end if;
    return new;
  end if;

  -- convert_to rather than digest(text, ...) so that text and bytes are hashed
  -- the same way and a page's hash does not depend on the database encoding.
  -- 120_binary_test.sql asserts the two agree on UTF8, which is what makes
  -- this the same value the generated column produced.
  body := coalesce(new.content_bytes, convert_to(new.content, 'UTF8'));
  if body is null then
    new.byte_size := null;
    new.content_hash := null;
    return new;
  end if;

  if new.content_bytes is not null then
    cap := wiki.max_attachment_bytes();
    if octet_length(body) > cap then
      -- 22001, string_data_right_truncation: a real SQLSTATE, so PostgREST
      -- answers 400 and a client can tell "too big" from "refused" without
      -- reading the sentence. The sentence is for the person.
      raise exception 'that file is % bytes; this wiki accepts at most %',
        octet_length(body), cap
        using errcode = '22001',
              hint = 'ask an operator to raise max_attachment_bytes';
    end if;
  end if;

  new.byte_size := octet_length(body);
  new.content_hash := digest(body, 'sha256');
  return new;
end;
$$;

create trigger document_version_body
  before insert or update on wiki.document_version
  for each row execute function wiki.version_fill_body();

-- The same cap on the way in through a draft, so a file too big to publish is
-- refused when it is written rather than at push time, when the person has
-- moved on and the mount reports a failure with no file in front of it.
create or replace function wiki.draft_check_body()
returns trigger
language plpgsql security definer
set search_path = wiki, public, pg_temp as $$
declare
  cap bigint;
begin
  if new.content_bytes is not null then
    cap := wiki.max_attachment_bytes();
    if octet_length(new.content_bytes) > cap then
      raise exception 'that file is % bytes; this wiki accepts at most %',
        octet_length(new.content_bytes), cap
        using errcode = '22001',
              hint = 'ask an operator to raise max_attachment_bytes';
    end if;
  end if;
  return new;
end;
$$;

create trigger draft_body_within_limit
  before insert or update of content_bytes on wiki.draft
  for each row execute function wiki.draft_check_body();

------------------------------------------------------------------------------
-- Putting a file there, and taking it away
------------------------------------------------------------------------------
--
-- Both are conveniences over machinery that already exists, and both are
-- SECURITY INVOKER so every policy applies exactly as it would to a client
-- doing it by hand. They save a round trip and a parent lookup; they are not
-- trusted with anything.
--
-- Note what `attach` is: `ensure_folder` plus `publish_revision`. A file is a
-- revision, so putting one there is publishing one -- and it therefore obeys
-- the same base_version check, writes the same history and appears in the same
-- point-in-time view as a page does.
create or replace function wiki.attach(p_path ltree,
                                       p_content_type text,
                                       p_bytes bytea,
                                       p_message text default null)
returns table (document_id uuid, version integer, byte_size bigint)
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  parent   uuid;
  existing wiki.document;
  target   uuid;
  live     integer;
  made     integer;
begin
  if nlevel(p_path) < 2 then
    raise exception 'a file needs a folder to live in' using errcode = '22023';
  end if;

  select * into existing from wiki.document where path = p_path;
  if found then
    if existing.is_folder then
      raise exception 'there is already a folder at %', p_path::text
        using errcode = '23505';
    end if;
    target := existing.id;
    select v.version into live from wiki.document_version v
     where v.document_id = target and upper_inf(v.valid);
  else
    parent := wiki.ensure_folder(subpath(p_path, 0, nlevel(p_path) - 1));
    insert into wiki.document (parent_id, slug, is_folder, title, owner_id,
                               created_by)
    values (parent, subpath(p_path, nlevel(p_path) - 1)::text, false,
            subpath(p_path, nlevel(p_path) - 1)::text,
            wiki.current_user_id(), wiki.current_user_id())
    returning id into target;
  end if;

  made := wiki.publish_revision(target, live, p_path, null, p_bytes,
                                p_content_type,
                                coalesce(p_message, 'attached'));
  return query
    select target, made, v.byte_size from wiki.document_version v
     where v.document_id = target and v.version = made;
end;
$$;

comment on function wiki.attach(ltree, text, bytea, text) is
  'Publish a binary revision at a path, creating the document and its folders '
  'if needed. ensure_folder plus publish_revision, so the base_version check, '
  'the history and the audit trail are the ones a page gets.';

-- Retiring rather than deleting, which is new. Under 140 an attachment had no
-- history, so removal had to be permanent and asked for `purge`. A binary
-- revision has history like any other, so this is an ordinary tombstone and
-- asks for `delete` -- and reinstating one is another revision rather than an
-- apology.
create or replace function wiki.detach(p_path ltree, p_message text default null)
returns boolean
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  target uuid;
  live   integer;
begin
  select d.id, v.version into target, live
    from wiki.document d
    left join wiki.document_version v
      on v.document_id = d.id and upper_inf(v.valid)
   where d.path = p_path and not d.is_folder;
  if target is null or live is null then
    return false;
  end if;
  perform wiki.publish_revision(target, live, p_path, null, null, null,
                                coalesce(p_message, 'detached'), true);
  return true;
end;
$$;

comment on function wiki.detach(ltree, text) is
  'Retire a file. A tombstone revision, so `delete` rather than `purge`, and '
  'the bytes stay in history where a page''s text would.';

------------------------------------------------------------------------------
-- Grants
------------------------------------------------------------------------------
--
-- `wiki.setting` appears nowhere, and that is the point of it.
-- `wiki.storage_backend` is readable, because a client deciding whether it can
-- fetch a body itself needs to know the name means something.
grant select on wiki.storage_backend to fswiki_user, fswiki_anon;

grant execute on function
    wiki.attach(ltree, text, bytea, text),
    wiki.detach(ltree, text),
    wiki.max_attachment_bytes()
  to fswiki_user;

grant execute on function wiki.max_attachment_bytes() to fswiki_anon;
