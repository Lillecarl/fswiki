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
-- Each statement below is its own transaction, and that is the point: the
-- counter is bumped inside the writing transaction, so its new value becomes
-- visible when that transaction commits and not while it runs. Sampling both
-- sides inside one DO block sees no movement at all. That is the right
-- behaviour for a change token — it becomes visible exactly when the change
-- does — but it means this test cannot be written as a single block.
create temporary table wiki_test_token as select wiki.change_token() as t;

update wiki.document set title = title where path = 'root.public';

select wiki_test.expect('change_token moves after a committed write',
  wiki.change_token() is distinct from (select t from wiki_test_token),
  format('before=%s after=%s', (select t from wiki_test_token), wiki.change_token()));

drop table wiki_test_token;

-- The invariant the counter exists for: writing down that somebody read
-- something is not a change to what anybody can read. If either of these ever
-- moves the token, an impersonated mount is back to refetching the whole
-- manifest on every poll -- because the impersonation hook writes one of them
-- on every impersonated request.
--
-- The ids are minted up front so the rows can be removed again afterwards; the
-- files after this one count what is in these tables.
create temporary table wiki_test_token as
  select wiki.change_token() as t,
         gen_random_uuid()   as probe_event,
         gen_random_uuid()   as probe_session;

insert into wiki.access_event (event_id, document_id, principal_id, action,
                               occurred_at)
select (select probe_event from wiki_test_token), d.id, p.id, 'open', now()
  from wiki.document d, wiki.principal p
 where d.path = 'root.public.welcome' and p.name = 'bob';

insert into wiki.impersonation_event (id, actor_id, subject_id)
select (select probe_session from wiki_test_token), a.id, s.id
  from wiki.principal a, wiki.principal s
 where a.name = 'dave' and s.name = 'bob';

-- The session bump specifically: one UPDATE per impersonated request, and the
-- write that used to move the token every single time.
update wiki.impersonation_event
   set requests = requests + 1, last_seen_at = now()
 where id = (select probe_session from wiki_test_token);

select wiki_test.expect('the audit trail does not move the change token',
  wiki.change_token() is not distinct from (select t from wiki_test_token),
  format('before=%s after=%s', (select t from wiki_test_token), wiki.change_token()));

delete from wiki.access_event
 where event_id = (select probe_event from wiki_test_token);
delete from wiki.impersonation_event
 where id = (select probe_session from wiki_test_token);
drop table wiki_test_token;

-- And fswiki_user must not be able to move it by hand. The token is only worth
-- polling if it means what it says.
select wiki_test.expect_eq('fswiki_user may read the counter',
  has_table_privilege('fswiki_user', 'wiki.change_counter', 'select'),
  true);

select wiki_test.expect_eq('fswiki_user may not write the counter',
  has_table_privilege('fswiki_user', 'wiki.change_counter', 'update')
    or has_table_privilege('fswiki_user', 'wiki.change_counter', 'insert')
    or has_table_privilege('fswiki_user', 'wiki.change_counter', 'delete'),
  false);
