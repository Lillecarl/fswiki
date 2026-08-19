

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
