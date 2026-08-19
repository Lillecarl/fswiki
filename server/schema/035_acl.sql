-- Access control entries.
--
-- Modelled on NTFS DACLs rather than on scoped grants: an ACE is attached to one
-- document, names one principal (user or group), carries one role, and is either
-- allow or deny. Documents below it inherit it unless inheritance is blocked.
--
-- Because an ACE belongs to a document id rather than to a path, explicit
-- permissions follow a document when it is moved — and it re-inherits from
-- wherever it lands. That is the NTFS behaviour, and it is the reason this model
-- beats a path-scoped grant table.
--
-- INHERITANCE FLAGS (the four NTFS ACE flags, same names, same meanings):
--
--   container_inherit (CI)  the ACE reaches descendant folders
--   object_inherit    (OI)  the ACE reaches descendant documents
--   inherit_only      (IO)  the ACE does NOT apply to the document it sits on
--   no_propagate      (NP)  the ACE reaches immediate children only
--
-- The defaults (CI+OI, not IO, not NP) give the ordinary "this folder,
-- subfolders and files" behaviour people expect when they tick a box.

create type wiki.ace_type as enum ('allow', 'deny');

create table wiki.ace (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid not null references wiki.document(id) on delete cascade,
  principal_id  uuid not null references wiki.principal(id) on delete cascade,
  role_id       uuid not null references wiki.role(id) on delete restrict,
  ace_type      wiki.ace_type not null,

  container_inherit boolean not null default true,
  object_inherit    boolean not null default true,
  inherit_only      boolean not null default false,
  no_propagate      boolean not null default false,

  created_at    timestamptz not null default now(),
  created_by    uuid references wiki.principal(id) on delete set null,
  expires_at    timestamptz,
  note          text,

  -- An ACE that applies to nothing is a configuration mistake, not a no-op.
  constraint ace_applies_somewhere check (
    not inherit_only or container_inherit or object_inherit
  ),
  constraint ace_key unique (document_id, principal_id, role_id, ace_type)
);

create index ace_document_idx  on wiki.ace (document_id);
create index ace_principal_idx on wiki.ace (principal_id);

comment on table wiki.ace is
  'One access control entry. Evaluation order is nearest-first, deny-before-allow '
  'at equal distance; see wiki.resolve_ace().';
