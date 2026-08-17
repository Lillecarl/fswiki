-- Principals: the things a grant can be handed to.
--
-- Users and groups share one table so that `access_grant.principal_id` is a
-- single FK rather than a nullable pair. Group membership is transitive:
-- groups may contain groups.

create type wiki.principal_kind as enum ('user', 'group');

create table wiki.principal (
  id          uuid primary key default gen_random_uuid(),
  kind        wiki.principal_kind not null,
  name        text not null,
  created_at  timestamptz not null default now(),

  constraint principal_name_not_blank check (length(trim(name)) > 0),
  constraint principal_kind_name_key  unique (kind, name),
  -- Lets dependent tables constrain *which kind* of principal they reference
  -- via a composite FK, instead of needing a trigger.
  constraint principal_id_kind_key    unique (id, kind)
);

-- Identity for principals of kind 'user'.
--
-- Group membership deliberately does NOT come from the IdP token. Claims baked
-- into a JWT stay valid until it expires, so a revocation would not take effect
-- until then. Membership is relational and resolved per query; sync it from the
-- IdP on a schedule if you like, but the database is the authority.
create table wiki.user_account (
  principal_id  uuid primary key references wiki.principal(id) on delete cascade,
  principal_kind wiki.principal_kind not null generated always as ('user') stored,

  oidc_issuer   text not null,
  oidc_subject  text not null,
  email         text,
  display_name  text,
  is_active     boolean not null default true,
  -- Bypasses every grant check. Keep the count near zero.
  is_superuser  boolean not null default false,
  created_at    timestamptz not null default now(),
  last_seen_at  timestamptz,

  constraint user_account_oidc_key unique (oidc_issuer, oidc_subject),
  constraint user_account_is_user
    foreign key (principal_id, principal_kind) references wiki.principal(id, kind)
);

create unique index user_account_email_key
  on wiki.user_account (lower(email)) where email is not null;

-- Nested group membership. `member_id` may be a user or a group.
create table wiki.group_member (
  group_id    uuid not null,
  group_kind  wiki.principal_kind not null generated always as ('group') stored,
  member_id   uuid not null references wiki.principal(id) on delete cascade,
  added_at    timestamptz not null default now(),
  added_by    uuid references wiki.principal(id) on delete set null,

  primary key (group_id, member_id),
  constraint group_member_no_self check (group_id <> member_id),
  constraint group_member_group_is_group
    foreign key (group_id, group_kind) references wiki.principal(id, kind)
      on delete cascade
);

create index group_member_member_idx on wiki.group_member (member_id);

-- Membership cycles would make the recursive expansion in wiki.effective_principals
-- non-terminating in spirit (UNION saves us, but the result is nonsense), so
-- reject them at write time.
create or replace function wiki.group_member_reject_cycle()
returns trigger
language plpgsql as $$
begin
  if exists (
    with recursive ancestors as (
      select new.group_id as id
      union
      select gm.group_id
        from wiki.group_member gm
        join ancestors a on gm.member_id = a.id
    )
    select 1 from ancestors where id = new.member_id
  ) then
    raise exception 'group membership cycle: % is already an ancestor of %',
      new.member_id, new.group_id
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger group_member_no_cycles
  before insert or update on wiki.group_member
  for each row execute function wiki.group_member_reject_cycle();
