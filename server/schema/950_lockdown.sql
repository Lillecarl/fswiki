-- Close the default that PostgreSQL leaves open. Loads last, on purpose.
--
-- PostgreSQL grants EXECUTE on every newly created function to PUBLIC. There is
-- no way to create a function that is not world-executable; you can only take
-- it back afterwards. Combined with the `usage on schema wiki` that
-- fswiki_anon needs to exist at all, that meant an **unauthenticated** caller
-- could invoke the entire ACL engine over PostgREST.
--
-- It is worse than it sounds, because these functions are SECURITY DEFINER —
-- they read the ACL tables as the owner, bypassing RLS — and they take the
-- principal as an explicit argument that defaults to the caller. PostgREST
-- lets a client supply any named argument, so anon could ask:
--
--   POST /rpc/capabilities_at {"p_document":"<uuid>","p_user":"<bob's uuid>"}
--   -> ["read","write","create"]
--
-- That is another principal's effective permissions on a document anon may not
-- be told exists, from a caller holding no token at all. Verified against a
-- live PostgREST before this file was written.
--
-- This must run after every function is defined, which is why it is 950 and not
-- part of 060_roles.sql: a revoke there would miss everything in 070, 075 and
-- 080. Revoking from PUBLIC does not disturb the explicit grants those files
-- make to fswiki_user.

revoke execute on all functions in schema wiki from public;

-- Belt and braces for anything added later without thinking about it.
alter default privileges in schema wiki revoke execute on functions from public;

-- RESIDUAL, KNOWN AND ACCEPTED
-- ----------------------------
-- An *authenticated* caller can still pass an explicit p_user to the functions
-- fswiki_user must hold EXECUTE on — wiki.can(), wiki.can_traverse(),
-- wiki.has_capability(), wiki.capabilities_at() — and learn another principal's
-- capabilities on a document whose uuid they know.
--
-- These cannot simply be revoked: RLS policy expressions are evaluated with the
-- querying role's privileges, so removing the grant makes every policy that
-- calls them fail with "permission denied for function" rather than filtering.
-- That was measured, not assumed.
--
-- The disclosure is close to what `ace_select` grants on purpose — anyone who
-- can read a document can read its ACL — and document uuids are not guessable,
-- so this is a small leak rather than an open door. Closing it properly means
-- splitting each function into an internal form and a no-p_user client form,
-- and pointing the views at the latter. Worth doing; not worth doing quietly in
-- the same change as the anon fix.
