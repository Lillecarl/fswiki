

-- Database roles for PostgREST.
--
--   fswiki_authenticator  -- what PostgREST connects as; can do nothing itself
--   fswiki_anon           -- unauthenticated requests; sees nothing
--   fswiki_user           -- every authenticated wiki user
--
-- All three are NOLOGIN except the authenticator. Note that fswiki_user is a
-- single database role shared by every human: separation comes from RLS reading
-- the JWT, not from one Postgres role per person. That is what keeps the
-- connection pool useful.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'fswiki_anon') then
    create role fswiki_anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'fswiki_user') then
    create role fswiki_user nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'fswiki_authenticator') then
    create role fswiki_authenticator noinherit login;
  end if;
end
$$;
