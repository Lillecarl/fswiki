-- Attachments: the limit, the flag that cannot lie, and the two RPCs.
--
-- The table and the reasoning behind it are in tables/140_attachments.sql.
-- Read that first; this file is the moving parts.

------------------------------------------------------------------------------
-- The configurable limit
------------------------------------------------------------------------------
--
-- SECURITY DEFINER because `wiki.setting` is granted to no client role. The
-- *value* is not a secret -- the error below names it, because a refusal a
-- person cannot act on is a bug -- but the row must not be writable by anyone
-- the wiki serves.
--
-- The fallback is not a formality. `seed/140_attachments.sql` inserts the
-- default, but a database somebody has been editing by hand may not have it,
-- and an attachment table with no working limit is worse than a conservative
-- one.
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
  'The upload cap, in bytes. From wiki.setting, which no client role may read '
  'or write; 10 MiB if the row is missing.';

create or replace function wiki.attachment_check_size()
returns trigger
language plpgsql security definer
set search_path = wiki, public, pg_temp as $$
declare
  cap bigint := wiki.max_attachment_bytes();
begin
  if octet_length(new.bytes) > cap then
    -- 22001, string_data_right_truncation. A real SQLSTATE rather than a bare
    -- raise, so PostgREST answers 400 and a client can tell "too big" from
    -- "refused" without reading the sentence. The sentence is for the person.
    raise exception
      'attachment is % bytes; this wiki accepts at most %',
      octet_length(new.bytes), cap
      using errcode = '22001',
            hint = 'ask an operator to raise max_attachment_bytes';
  end if;
  return new;
end;
$$;

create trigger attachment_within_limit
  before insert or update of bytes on wiki.attachment
  for each row execute function wiki.attachment_check_size();

------------------------------------------------------------------------------
-- document.is_attachment follows this table, rather than being told
------------------------------------------------------------------------------
--
-- The flag exists so that `current_document` can say which of the three kinds
-- a row is without joining to `wiki.attachment` -- a join there would put that
-- table's row policy on every manifest, which is one ACL walk per attachment
-- per request. See tables/140.
--
-- Maintained here rather than by whoever writes the row, so the two cannot
-- disagree. SECURITY DEFINER for the same reason: clearing the flag on delete
-- must work for a caller who holds `purge` on the document but not `write`,
-- and a flag that fails to clear is a page that renders as an empty file
-- forever.
create or replace function wiki.attachment_mark_document()
returns trigger
language plpgsql security definer
set search_path = wiki, public, pg_temp as $$
begin
  if tg_op = 'DELETE' then
    -- The document may be going too, by cascade. Updating a row that is about
    -- to disappear is harmless; not updating one that is staying is not.
    update wiki.document set is_attachment = false where id = old.document_id;
    return old;
  end if;
  update wiki.document set is_attachment = true where id = new.document_id;
  return new;
end;
$$;

create trigger attachment_marks_its_document
  after insert or delete on wiki.attachment
  for each row execute function wiki.attachment_mark_document();

------------------------------------------------------------------------------
-- Putting one there
------------------------------------------------------------------------------
--
-- SECURITY INVOKER, so every policy applies exactly as it would to a client
-- doing this by hand: `create` on the parent folder to make the document,
-- `write` to replace the bytes of one that is already there. This function
-- exists for the round trip and the parent lookup, not to be trusted with
-- anything.
--
-- Replacing rather than versioning. An attachment has no history -- see
-- tables/140 for why not -- so a second upload to the same path overwrites,
-- and the caller learns which happened from `created` in the result.
create or replace function wiki.attach(p_path ltree,
                                       p_media_type text,
                                       p_bytes bytea)
returns table (document_id uuid, created boolean, byte_size integer)
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  parent_path ltree;
  parent      uuid;
  existing    wiki.document;
  target      uuid;
  fresh       boolean := false;
begin
  if nlevel(p_path) < 2 then
    raise exception 'an attachment needs a folder to live in'
      using errcode = '22023';
  end if;
  parent_path := subpath(p_path, 0, nlevel(p_path) - 1);

  select * into existing from wiki.document where path = p_path;
  if found then
    -- A page and a file cannot share an address. Refused here with a sentence
    -- rather than left to the unique constraint, which would say
    -- `document_path_key` and mean nothing to the person holding the file.
    if not existing.is_attachment then
      raise exception 'there is already a page at %', p_path::text
        using errcode = '23505',
              hint = 'attachments and pages share one path space';
    end if;
    target := existing.id;
    update wiki.attachment
       set bytes = p_bytes, media_type = p_media_type,
           updated_at = now(), updated_by = wiki.current_user_id()
     where wiki.attachment.document_id = target;
    if not found then
      raise exception 'not yours to replace' using errcode = '42501';
    end if;
  else
    select id into parent from wiki.document where path = parent_path;
    if parent is null then
      -- Same answer as everywhere else: a folder you may not see and a folder
      -- that is not there are one message, because telling them apart is how
      -- the tree leaks.
      raise exception 'no folder at %', parent_path::text
        using errcode = '23503';
    end if;
    insert into wiki.document (parent_id, slug, is_folder, title, owner_id,
                               created_by)
    values (parent, subpath(p_path, nlevel(p_path) - 1)::text, false,
            subpath(p_path, nlevel(p_path) - 1)::text,
            wiki.current_user_id(), wiki.current_user_id())
    returning id into target;
    insert into wiki.attachment (document_id, bytes, media_type,
                                 created_by, updated_by)
    values (target, p_bytes, p_media_type,
            wiki.current_user_id(), wiki.current_user_id());
    fresh := true;
  end if;

  return query
    select target, fresh, a.byte_size from wiki.attachment a
     where a.document_id = target;
end;
$$;

comment on function wiki.attach(ltree, text, bytea) is
  'Store or replace an attachment. SECURITY INVOKER: `create` on the parent to '
  'make one, `write` to replace one, and the size trigger either way.';

-- Removing one is a delete, not a tombstone, because there is no history to
-- keep -- so it asks for `purge` through the document policy rather than
-- `delete`. Saying that out loud is better than a `delete` that turns out to
-- be irreversible.
create or replace function wiki.detach(p_path ltree)
returns boolean
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  gone integer;
begin
  delete from wiki.document d
   where d.path = p_path and d.is_attachment;
  get diagnostics gone = row_count;
  return gone > 0;
end;
$$;

comment on function wiki.detach(ltree) is
  'Remove an attachment permanently. Needs `purge`: an attachment has no '
  'history, so there is no retire to offer instead.';

------------------------------------------------------------------------------
-- Reading one
------------------------------------------------------------------------------
--
-- By path, because that is what a browser has, and as an RPC because a GET
-- runs read-only and impersonation refuses a transaction it cannot log in --
-- the same argument as wiki.document_at(). SECURITY INVOKER over both tables,
-- so it is the same read RLS would have given, not a second opinion.
--
-- Deliberately not audited. `wiki.view_document` records a page view because a
-- person chose to read that page; a browser fetches every image on a page as a
-- subresource, so auditing these would bury the trail in requests nobody made
-- on purpose. The page that referenced the file is what was read, and that is
-- already recorded.
create or replace function wiki.attachment_at(p_path ltree)
returns table (document_id uuid, path ltree, media_type text,
               byte_size integer, sha256 bytea, bytes bytea,
               updated_at timestamptz)
language sql volatile
set search_path = wiki, public, pg_temp as $$
  select a.document_id, d.path, a.media_type, a.byte_size, a.sha256, a.bytes,
         a.updated_at
    from wiki.attachment a
    join wiki.document d on d.id = a.document_id
   where d.path = p_path;
$$;

comment on function wiki.attachment_at(ltree) is
  'One attachment''s bytes by path, or no rows. SECURITY INVOKER, so a file '
  'you may not read is a file that is not there.';

------------------------------------------------------------------------------
-- Grants
------------------------------------------------------------------------------
--
-- `wiki.setting` appears nowhere below, and that is the point of it.
--
-- An anonymous caller gets `attachment_at` and nothing else. It is
-- SECURITY INVOKER, so it returns what `public` was granted and no more -- the
-- same standing `wiki.search` has. Writing needs an account.
grant select on wiki.attachment to fswiki_user, fswiki_anon;
grant insert, update, delete on wiki.attachment to fswiki_user;

grant execute on function
    wiki.attach(ltree, text, bytea),
    wiki.detach(ltree),
    wiki.attachment_at(ltree),
    wiki.max_attachment_bytes()
  to fswiki_user;

grant execute on function
    wiki.attachment_at(ltree),
    wiki.max_attachment_bytes()
  to fswiki_anon;
