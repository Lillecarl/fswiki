-- 900_builtin_roles.sql, the runtime half.
-- The file header, and the reasoning, are in ../seed/900_builtin_roles.sql.

-- Nobody may own a document as `public`.
--
-- wiki.can() gives a document's owner a standing `grant` right -- the ability
-- to repair an ACL they have locked themselves out of -- and it matches the
-- owner against the caller's effective principals, so a *group* owner confers
-- it on that group's members. `public` is in everybody's effective principals
-- by construction, which would make an owning `public` mean "anyone at all may
-- re-ACL this", anonymous callers included.
--
-- That is reachable from `write` alone: the document_update policy gates on
-- 'write', and owner_id is an ordinary column. So a user with permission to
-- edit one page could hand control of it to the internet. Refuse it at the
-- table, which is the only place that covers every route in -- the push RPC,
-- a hand-rolled PostgREST client, and psql alike.
--
-- Narrow on purpose: group ownership in general is a real feature and stays.
-- Only the group that everyone is in is refused.
create or replace function wiki.reject_public_owner()
returns trigger
language plpgsql
set search_path = wiki, public, pg_temp as $$
begin
  if exists (select 1 from wiki.principal p
              where p.id = new.owner_id
                and p.kind = 'group' and p.name = 'public') then
    raise exception 'the public group may not own a document'
      using errcode = 'insufficient_privilege';
  end if;
  return new;
end;
$$;

create trigger document_reject_public_owner
  before insert or update of owner_id on wiki.document
  for each row execute function wiki.reject_public_owner();
