-- 020_rbac.sql, the runtime half.
-- The file header, and the reasoning, are in ../tables/020_rbac.sql.

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
