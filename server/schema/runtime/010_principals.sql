-- 010_principals.sql, the runtime half.
-- The file header, and the reasoning, are in ../tables/010_principals.sql.

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
