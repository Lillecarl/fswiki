-- Built-in wiki roles.
--
-- Each role declares only the capabilities it *adds*. Everything below them
-- arrives two ways and both are load-bearing:
--
--   role inheritance     names the bundle    (maintainer is an editor plus more)
--   capability requires  closes the lattice  (write drags read along)
--
--   reader     -> read, sync
--   author     -> reader + create
--   editor     -> author + write
--   maintainer -> editor + delete + grant
--   owner      -> maintainer + administer
--
-- `sync` sits in `reader` so documents are syncable by default; take it away
-- with a deny ACE carrying the `sync` role where you want browser-only access.

insert into wiki.role (name, description, is_builtin) values
  ('reader',     'Read documents, and mirror them locally',              true),
  ('author',     'Read, and create new documents',                       true),
  ('editor',     'Read, create and modify documents',                    true),
  ('maintainer', 'Full content control, and may delegate access',        true),
  ('owner',      'Everything short of permanent destruction',            true),
  ('sync',       'Mirror documents to a local filesystem. Deny this to '
                 'force browser-only access and keep the audit trail.',  true),
  ('purger',     'Destroy documents and their history, irrecoverably. '
                 'In no inherited role: grant it deliberately.',         true)
on conflict (name) do nothing;

insert into wiki.role_capability (role_id, capability)
select r.id, c.capability::wiki.capability
  from wiki.role r
  join (values
      ('reader',     'read'),
      ('reader',     'sync'),
      ('author',     'create'),
      ('editor',     'write'),
      ('maintainer', 'delete'),
      ('maintainer', 'grant'),
      ('owner',      'administer'),
      ('sync',       'sync'),
      ('purger',     'purge')
    ) as c(role_name, capability)
    on c.role_name = r.name
on conflict do nothing;

insert into wiki.role_inherits (role_id, inherits_role_id)
select child.id, parent.id
  from (values
      ('author',     'reader'),
      ('editor',     'author'),
      ('maintainer', 'editor'),
      ('owner',      'maintainer')
    ) as e(child_name, parent_name)
  join wiki.role child  on child.name  = e.child_name
  join wiki.role parent on parent.name = e.parent_name
on conflict do nothing;

-- The built-in group everybody is in.
--
-- A group with no members and no way to gain any: wiki.effective_principals()
-- returns it for every caller, which is the whole mechanism. Granting a role
-- on a document to `public` is how a page becomes readable without an account,
-- and a deny ACE naming it is how one stops being.
--
-- It is a group and not a user on purpose. A principal of kind 'user' would
-- make wiki.current_user_id() non-NULL for anonymous requests, and eight
-- policies in 050_rls.sql admit any caller for whom that is true -- the user
-- directory, group membership and the role tables among them. Nobody is a
-- member of `public`, so there is nothing for a membership check to leak.
insert into wiki.principal (kind, name) values ('group', 'public')
on conflict (kind, name) do nothing;

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

-- The root of the tree. Everything else hangs off this; a wiki-wide grant is
-- simply scope = 'root'.
insert into wiki.document (id, parent_id, slug, is_folder, title)
select gen_random_uuid(), null, 'root', true, 'Wiki'
 where not exists (select 1 from wiki.document where path = 'root'::ltree);
