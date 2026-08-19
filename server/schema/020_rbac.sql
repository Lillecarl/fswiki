-- Roles: named bundles of capabilities.
--
-- These are the equivalent of the Windows permission presets ("Read", "Modify",
-- "Full control") rather than of a grant: a role says *what* a principal may do,
-- and an ACE (see 035_acl.sql) says *where* and to *whom*. Roles compose by
-- inheritance so "maintainers can do everything editors can" holds by
-- construction instead of by copy-paste.

-- Declaration order is the sort order everywhere capabilities are listed, so it
-- runs weakest to strongest.
create type wiki.capability as enum (
  'read',       -- see that the document exists, and read its content
  'sync',       -- take a local copy via FUSE or the CLI (see below)
  'write',      -- change an existing document
  'create',     -- add new documents inside this folder
  'delete',     -- retire a document: writes a tombstone, recoverable
  'grant',      -- change the ACL  (Windows: WRITE_DAC)
  'administer', -- take ownership, move subtrees
  'purge'       -- destroy a document and its history, irrecoverably
);

-- Capabilities form a requirement DAG rather than a flat set: there is no write
-- without read. Encoding it here rather than relying on roles to spell it out
-- means the implication holds however the ACL is assembled, including for
-- principals whose access arrives from several ACEs.
--
-- It cuts both ways, and the two directions are not the same closure:
--
--   allow X  =>  also allows everything X requires  (downward)
--   deny  X  =>  also denies everything requiring X (upward, the contrapositive)
--
-- So denying `read` denies writing too, which is the whole point. See
-- wiki.ace_covers().
--
-- `delete` sits low, directly on `read`, because everything is versioned: it
-- writes a tombstone and the content is still there. Retiring a page you may
-- not edit is a reasonable thing to let a librarian do, so it does not require
-- `write`. Actual destruction is `purge`, which sits alone at the top and is in
-- no inherited role — it has to be granted on purpose.
create table wiki.capability_requires (
  capability  wiki.capability not null,
  requires    wiki.capability not null,
  primary key (capability, requires),
  constraint capability_requires_no_self check (capability <> requires)
);

insert into wiki.capability_requires (capability, requires) values
  ('write',      'read'),
  ('create',     'read'),
  ('delete',     'read'),
  ('grant',      'read'),
  ('administer', 'grant'),
  ('purge',      'delete'),
  ('purge',      'administer'),
  -- `sync` requires read but nothing requires `sync`: denying it leaves a
  -- document readable in the browser while keeping it off local disks. That is
  -- the audit-trail lever — every view is a request the server sees, instead of
  -- one bulk copy followed by silence.
  ('sync',       'read');

create or replace function wiki.capability_requires_reject_cycle()
returns trigger
language plpgsql as $$
begin
  if exists (
    with recursive reachable as (
      select new.requires as cap
      union
      select cr.requires
        from wiki.capability_requires cr
        join reachable r on cr.capability = r.cap
    )
    select 1 from reachable where cap = new.capability
  ) then
    raise exception 'capability requirement cycle: % already requires %',
      new.requires, new.capability
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger capability_requires_no_cycles
  before insert or update on wiki.capability_requires
  for each row execute function wiki.capability_requires_reject_cycle();

create table wiki.role (
  id           uuid primary key default gen_random_uuid(),
  name         text not null unique,
  description  text,
  -- Built-in roles are referenced by name from application code and must not
  -- be deleted; see the delete guard below.
  is_builtin   boolean not null default false,
  created_at   timestamptz not null default now(),

  constraint role_name_shape check (name ~ '^[a-z][a-z0-9_]*$')
);

create table wiki.role_capability (
  role_id     uuid not null references wiki.role(id) on delete cascade,
  capability  wiki.capability not null,
  primary key (role_id, capability)
);

-- Role hierarchy. `role_id` inherits every capability of `inherits_role_id`,
-- transitively. Kept separate from role_capability so a role can be pure
-- composition (e.g. `maintainer` = `editor` + `grant`) with no direct caps.
create table wiki.role_inherits (
  role_id           uuid not null references wiki.role(id) on delete cascade,
  inherits_role_id  uuid not null references wiki.role(id) on delete cascade,
  primary key (role_id, inherits_role_id),
  constraint role_inherits_no_self check (role_id <> inherits_role_id)
);

create index role_inherits_parent_idx on wiki.role_inherits (inherits_role_id);

create or replace function wiki.role_inherits_reject_cycle()
returns trigger
language plpgsql as $$
begin
  if exists (
    with recursive descendants as (
      select new.inherits_role_id as id
      union
      select ri.inherits_role_id
        from wiki.role_inherits ri
        join descendants d on ri.role_id = d.id
    )
    select 1 from descendants where id = new.role_id
  ) then
    raise exception 'role inheritance cycle: % already inherits from %',
      new.inherits_role_id, new.role_id
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger role_inherits_no_cycles
  before insert or update on wiki.role_inherits
  for each row execute function wiki.role_inherits_reject_cycle();

create or replace function wiki.role_reject_builtin_delete()
returns trigger
language plpgsql as $$
begin
  raise exception 'role % is built in and cannot be deleted', old.name
    using errcode = 'restrict_violation';
end;
$$;

create trigger role_builtin_undeletable
  before delete on wiki.role
  for each row when (old.is_builtin)
  execute function wiki.role_reject_builtin_delete();

------------------------------------------------------------------------------
-- Strict lookups
------------------------------------------------------------------------------

-- Capabilities are an enum, so `'read'` is checked when the statement is
-- parsed and a typo is a syntax-time error. Roles and principals are *rows*,
-- so `where name = 'editior'` is an unchecked string that quietly matches
-- nothing — the insert selects zero rows and reports success. These turn that
-- into a loud failure at the point of use; prefer them over inline name
-- lookups everywhere outside of ad-hoc queries.
create or replace function wiki.role_id(p_name text)
returns uuid
language plpgsql stable security definer
set search_path = wiki, public, pg_temp as $$
declare found uuid;
begin
  select id into found from wiki.role where name = p_name;
  if found is null then
    raise exception 'no such role: %', p_name using errcode = 'no_data_found';
  end if;
  return found;
end;
$$;

create or replace function wiki.principal_id(p_kind wiki.principal_kind, p_name text)
returns uuid
language plpgsql stable security definer
set search_path = wiki, public, pg_temp as $$
declare found uuid;
begin
  select id into found from wiki.principal where kind = p_kind and name = p_name;
  if found is null then
    raise exception 'no such %: %', p_kind, p_name using errcode = 'no_data_found';
  end if;
  return found;
end;
$$;
