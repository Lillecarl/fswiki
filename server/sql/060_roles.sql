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

grant fswiki_anon, fswiki_user to fswiki_authenticator;

grant usage on schema wiki to fswiki_anon, fswiki_user;

-- The authenticator is NOINHERIT, so it does not pick this up from the roles
-- granted to it — and PostgREST loads its schema cache as the authenticator,
-- before any SET ROLE. Without this the cache comes back empty and every
-- request 404s. Usage on the schema is all it gets: no table privileges, so it
-- still cannot read a single row itself.
grant usage on schema wiki to fswiki_authenticator;

-- Table privileges are the coarse gate; RLS is the fine one. A user role that
-- can SELECT every table still only sees rows its policies admit.
grant select on
    wiki.document, wiki.document_version, wiki.ace, wiki.draft,
    wiki.principal, wiki.user_account, wiki.group_member,
    wiki.role, wiki.role_capability, wiki.role_inherits
  to fswiki_user;

grant insert, update, delete on wiki.draft            to fswiki_user;
grant insert, update, delete on wiki.ace              to fswiki_user;
grant insert, update, delete on wiki.document         to fswiki_user;
grant update (email, display_name, last_seen_at) on wiki.user_account to fswiki_user;

-- Publishing is a normal client privilege gated by normal policies, not a
-- capability hidden behind a definer function. Only `valid` is updatable, and
-- the document_version_immutable trigger narrows that to closing a live
-- revision — so the worst a hand-rolled client can do is skip wiki.push()'s
-- conflict detection and clobber someone's edit. It cannot rewrite history and
-- it cannot escape the ACL.
grant insert          on wiki.document_version to fswiki_user;
grant update (valid)  on wiki.document_version to fswiki_user;

-- RLS policies are checked against the *querying* role, so fswiki_user needs
-- EXECUTE on every function its policies touch — a missing grant here surfaces
-- as "permission denied for function", not as a silent empty result. The same
-- goes for wiki.push(), which is SECURITY INVOKER by design.
--
-- Functions called only from *inside* a SECURITY DEFINER function are checked
-- against that function's owner, so they need no grant here.
grant execute on function
    wiki.role_id(text),
    wiki.principal_id(wiki.principal_kind, text),
    wiki.current_user_id(),
    wiki.effective_principals(uuid),
    wiki.role_capabilities(uuid),
    wiki.capability_downward(wiki.capability),
    wiki.capability_upward(wiki.capability),
    wiki.ace_covers(uuid, wiki.capability, wiki.ace_type),
    wiki.acl_chain(uuid),
    wiki.acl_chain(ltree),
    wiki.resolve_ace(ltree, boolean, wiki.capability, uuid),
    wiki.can(ltree, boolean, uuid, wiki.capability, uuid),
    wiki.has_capability(uuid, wiki.capability, uuid),
    wiki.can_traverse(uuid, wiki.capability, uuid),
    wiki.can_traverse(ltree, wiki.capability, uuid),
    wiki.capabilities_at(uuid, uuid),
    wiki.explain_acl(uuid, uuid)
  to fswiki_user;

grant select on wiki.capability_requires to fswiki_user;

-- wiki.change_token() is granted in 075_changes.sql, beside its definition.

-- Anonymous requests get a usable connection and nothing else; every policy
-- keys off wiki.current_user_id(), which is NULL without a token.
revoke all on all tables in schema wiki from fswiki_anon;
