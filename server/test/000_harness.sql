-- Minimal assertion harness. Results are collected rather than raised so that
-- one run reports every failure instead of stopping at the first.

create schema if not exists wiki_test;

create table if not exists wiki_test.result (
  seq    serial primary key,
  label  text not null,
  ok     boolean not null,
  detail text
);

-- How many assertions were *attempted*, as against how many were recorded.
--
-- A sequence because a sequence is the one thing in PostgreSQL a ROLLBACK does
-- not undo. `result` is an ordinary table, so an assertion made inside a
-- `begin; ... rollback;` block runs, decides, and then has its verdict thrown
-- away with everything else -- the suite reports a smaller total and says
-- nothing about the difference.
--
-- That is not hypothetical. Five assertions in 080_closure_test.sql spent
-- their whole life inside such a block: they ran on every single run and no
-- run ever reported them. A failing one would have been silent.
--
-- 999_harness_test.sql compares the two numbers, so losing a verdict is now
-- itself a failing assertion rather than a smaller total nobody was counting.
create sequence if not exists wiki_test.attempted;

-- SECURITY DEFINER so assertions still record while the session has SET ROLE to
-- an unprivileged wiki role.
create or replace function wiki_test.expect(p_label text, p_ok boolean, p_detail text default null)
returns void
language plpgsql security definer
set search_path = wiki_test, pg_temp as $$
begin
  -- Before the insert, and outside any transaction the caller may roll back.
  perform nextval('wiki_test.attempted');
  insert into wiki_test.result (label, ok, detail)
  values (p_label, coalesce(p_ok, false), p_detail);
end;
$$;

create or replace function wiki_test.expect_eq(p_label text, p_actual anyelement, p_expected anyelement)
returns void
language plpgsql security definer
set search_path = wiki_test, pg_temp as $$
begin
  perform wiki_test.expect(
    p_label,
    p_actual is not distinct from p_expected,
    format('expected %L, got %L', p_expected, p_actual));
end;
$$;

-- Assert that a statement is refused by an RLS policy specifically. A policy
-- violation on INSERT/UPDATE surfaces as insufficient_privilege; anything else
-- coming back means the statement failed for the wrong reason and the test has
-- not proved what it claims.
--
-- SECURITY INVOKER is load-bearing: as a definer function this executes the
-- statement as the table owner, who bypasses RLS, so every negative test
-- "succeeds" and the suite reports a hole as a pass. Recording the result still
-- works because wiki_test.expect() is definer.
create or replace function wiki_test.expect_denied(p_label text, p_sql text)
returns void
language plpgsql security invoker
set search_path = wiki, wiki_test, public, pg_temp as $$
begin
  begin
    execute p_sql;
    perform wiki_test.expect(p_label, false, 'statement unexpectedly succeeded');
  exception
    when insufficient_privilege then
      perform wiki_test.expect(p_label, true, 'rejected by policy');
    when others then
      perform wiki_test.expect(p_label, false, format('wrong error: %s (%s)', sqlerrm, sqlstate));
  end;
end;
$$;

-- The SQLSTATE a statement fails with, or NULL if it succeeded.
--
-- expect_rejected() records its verdict by writing to wiki_test.result, which a
-- read-only transaction forbids -- and a read-only transaction is exactly what
-- the impersonation tests need to make assertions about. This separates the
-- measuring from the recording so the measuring can happen anywhere.
create or replace function wiki_test.sqlstate_of(p_sql text)
returns text
language plpgsql security invoker
set search_path = wiki, wiki_test, public, pg_temp as $$
begin
  execute p_sql;
  return null;
exception when others then
  return sqlstate;
end;
$$;

-- Assert that a statement is refused by a constraint or trigger. Optionally
-- pins the SQLSTATE so a test cannot pass on an unrelated failure.
create or replace function wiki_test.expect_rejected(
  p_label text, p_sql text, p_sqlstate text default null)
returns void
language plpgsql security invoker
set search_path = wiki, wiki_test, public, pg_temp as $$
begin
  begin
    execute p_sql;
    perform wiki_test.expect(p_label, false, 'statement unexpectedly succeeded');
  exception
    when others then
      if p_sqlstate is null or sqlstate = p_sqlstate then
        perform wiki_test.expect(p_label, true, sqlerrm);
      else
        perform wiki_test.expect(p_label, false,
          format('expected SQLSTATE %s, got %s: %s', p_sqlstate, sqlstate, sqlerrm));
      end if;
  end;
end;
$$;

------------------------------------------------------------------------------
-- Lookup helpers
------------------------------------------------------------------------------

-- SECURITY DEFINER on purpose. As invoker functions these resolve under the
-- caller's own RLS, so a path the test subject cannot see comes back NULL and
-- every assertion built on it passes vacuously — which is exactly how the
-- owner-lockout test first "passed" while proving nothing.
create or replace function wiki_test.doc(p_path text)
returns uuid
language plpgsql stable security definer
set search_path = wiki, public, pg_temp as $$
declare found uuid;
begin
  select id into found from wiki.document where path = p_path::ltree;
  if found is null then
    raise exception 'no such document: %', p_path;
  end if;
  return found;
end;
$$;

create or replace function wiki_test.who(p_name text)
returns uuid
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select wiki.principal_id('user', p_name);
$$;

-- Switch the session to a wiki user, the way PostgREST does: verify the token,
-- stash its claims in a GUC, then drop to the low-privilege role.
-- who() is users only, so groups need their own lookup. Keeping them apart is
-- deliberate: a test that says who('everyone') has confused a group for a
-- person, and silently returning NULL would let it pass.
create or replace function wiki_test.grp(p_name text)
returns uuid
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select wiki.principal_id('group', p_name);
$$;

create or replace function wiki_test.login(p_subject text)
returns void
language plpgsql
set search_path = wiki_test, pg_temp as $$
begin
  perform set_config(
    'request.jwt.claims',
    json_build_object('iss', 'https://idp.test', 'sub', p_subject)::text,
    false);
end;
$$;

grant usage on schema wiki_test to fswiki_user;
grant execute on all functions in schema wiki_test to fswiki_user;

-- And for fswiki_anon, so the unauthenticated tests can record their verdicts
-- from inside SET ROLE. wiki_test is not in PGRST_DB_SCHEMAS, so this exists
-- only for the suite.
grant usage on schema wiki_test to fswiki_anon;
grant execute on all functions in schema wiki_test to fswiki_anon;
