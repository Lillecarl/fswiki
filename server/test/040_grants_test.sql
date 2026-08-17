-- Who may execute what.
--
-- PostgreSQL makes every new function executable by PUBLIC, and fswiki_anon has
-- USAGE on the wiki schema because it must in order to exist. Together those
-- meant an unauthenticated PostgREST caller could invoke the whole ACL engine —
-- SECURITY DEFINER functions, reading the ACL tables as the owner, with the
-- principal supplied as an argument. 950_lockdown.sql closes it. These
-- assertions are what stop it reopening the next time a function is added.

------------------------------------------------------------------------------
-- Anonymous callers execute nothing
------------------------------------------------------------------------------

select wiki_test.expect_eq('anon cannot execute capabilities_at',
  has_function_privilege('fswiki_anon',
    'wiki.capabilities_at(uuid, uuid)', 'execute'),
  false);

select wiki_test.expect_eq('anon cannot execute explain_acl',
  has_function_privilege('fswiki_anon',
    'wiki.explain_acl(uuid, uuid)', 'execute'),
  false);

select wiki_test.expect_eq('anon cannot execute has_capability',
  has_function_privilege('fswiki_anon',
    'wiki.has_capability(uuid, wiki.capability, uuid)', 'execute'),
  false);

select wiki_test.expect_eq('anon cannot execute current_user_id',
  has_function_privilege('fswiki_anon', 'wiki.current_user_id()', 'execute'),
  false);

select wiki_test.expect_eq('anon cannot execute change_token',
  has_function_privilege('fswiki_anon', 'wiki.change_token()', 'execute'),
  false);

select wiki_test.expect_eq('anon cannot execute push',
  has_function_privilege('fswiki_anon', 'wiki.push(text, ltree[])', 'execute'),
  false);

-- The generic form: nothing in the schema is left executable by PUBLIC. This is
-- the assertion that catches a function added later without a grant review.
select wiki_test.expect_eq('no wiki function is executable by PUBLIC',
  (select coalesce(array_agg(p.proname::text order by p.proname), '{}')
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'wiki'
      and has_function_privilege('public', p.oid, 'execute')),
  '{}'::text[]);

------------------------------------------------------------------------------
-- Authenticated callers keep exactly what RLS needs
------------------------------------------------------------------------------

-- RLS policy expressions are evaluated with the *querying* role's privileges,
-- so a missing grant here is not a subtle filtering change: every policy that
-- calls the function fails outright with "permission denied for function".
-- These four are load-bearing for 050_rls.sql and the views.
select wiki_test.expect_eq('fswiki_user may execute can()',
  has_function_privilege('fswiki_user',
    'wiki.can(ltree, boolean, uuid, wiki.capability, uuid)', 'execute'),
  true);

select wiki_test.expect_eq('fswiki_user may execute can_traverse()',
  has_function_privilege('fswiki_user',
    'wiki.can_traverse(ltree, wiki.capability, uuid)', 'execute'),
  true);

select wiki_test.expect_eq('fswiki_user may execute has_capability()',
  has_function_privilege('fswiki_user',
    'wiki.has_capability(uuid, wiki.capability, uuid)', 'execute'),
  true);

select wiki_test.expect_eq('fswiki_user may execute current_user_id()',
  has_function_privilege('fswiki_user', 'wiki.current_user_id()', 'execute'),
  true);

select wiki_test.expect_eq('fswiki_user may execute change_token()',
  has_function_privilege('fswiki_user', 'wiki.change_token()', 'execute'),
  true);

------------------------------------------------------------------------------
-- The change token itself
------------------------------------------------------------------------------

select wiki_test.expect('change_token returns something',
  wiki.change_token() is not null);

-- Only equality is meaningful, but it must differ after a write.
--
-- Each statement below is its own transaction, and that is the point: the WAL
-- position advances when a transaction commits, not while it runs. Sampling
-- both sides inside one DO block sees no movement at all. That is the right
-- behaviour for a change token — it becomes visible exactly when the change
-- does — but it means this test cannot be written as a single block.
create temporary table wiki_test_token as select wiki.change_token() as t;

update wiki.document set title = title where path = 'root.public';

select wiki_test.expect('change_token moves after a committed write',
  wiki.change_token() is distinct from (select t from wiki_test_token),
  format('before=%s after=%s', (select t from wiki_test_token), wiki.change_token()));

drop table wiki_test_token;
