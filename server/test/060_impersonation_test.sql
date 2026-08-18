-- Impersonation.
--
-- The claim under test is not "an admin can see more". It is that acting as
-- someone reproduces *their* view exactly -- and that acting as a membership is
-- a different thing from acting as the group, in both directions.
--
-- Everything behavioural here runs inside an explicit transaction, because
-- begin_impersonation locks that transaction read only and the lock is half the
-- feature. Observations are collected into temp tables (which a read-only
-- transaction still permits) and asserted afterwards, since wiki_test.result is
-- not a temp table and cannot be written while the lock is on.

------------------------------------------------------------------------------
-- Grants
------------------------------------------------------------------------------
--   dave  -> everyone      may act as any person, and may name `everyone`
--   dave  -> engineering   so dave can compose {everyone, engineering}
--   erin  -> engineering   engineering only: cannot compose the pair
--   bob   -> alice         an explicit grant over a superuser, which must not work

insert into wiki.impersonation_grant (actor_id, subject_id) values
  (wiki_test.who('dave'), wiki_test.grp('everyone')),
  (wiki_test.who('dave'), wiki_test.grp('engineering')),
  (wiki_test.who('erin'), wiki_test.grp('engineering')),
  (wiki_test.who('bob'),  wiki_test.who('alice'));

------------------------------------------------------------------------------
-- Who may act as whom
------------------------------------------------------------------------------

select wiki_test.expect_eq('a grant naming everyone covers every person',
  wiki.may_impersonate(wiki_test.who('dave'), wiki_test.who('bob')), true);

-- The guard that stops a diagnostic being a privilege escalation. dave holds a
-- grant that expands to cover alice; alice is a superuser; dave is not.
select wiki_test.expect_eq('never step up: dave may not act as a superuser',
  wiki.may_impersonate(wiki_test.who('dave'), wiki_test.who('alice')), false);

select wiki_test.expect_eq('nor with a grant naming the superuser outright',
  wiki.may_impersonate(wiki_test.who('bob'), wiki_test.who('alice')), false);

-- Superuser is not a bypass here. Seeing everything already, they gain only a
-- diagnostic, and a grant row is what makes it auditable.
select wiki_test.expect_eq('a superuser still needs a grant of their own',
  wiki.may_impersonate(wiki_test.who('alice'), wiki_test.who('bob')), false);

select wiki_test.expect_eq('nobody acts as themselves',
  wiki.may_impersonate(wiki_test.who('dave'), wiki_test.who('dave')), false);

select wiki_test.expect_eq('an ungranted actor is refused',
  wiki.may_impersonate(wiki_test.who('frank'), wiki_test.who('bob')), false);

select wiki_test.expect_eq('dave may name a granted group',
  wiki.may_impersonate_groups(wiki_test.who('dave'),
    array[wiki_test.grp('engineering')]), true);

select wiki_test.expect_eq('and may compose two of them',
  wiki.may_impersonate_groups(wiki_test.who('dave'),
    array[wiki_test.grp('everyone'), wiki_test.grp('engineering')]), true);

-- Every group in the set must be granted. Not "any", and not "the ones you are
-- not already in": dropping a group can drop a deny, so a subset of your own
-- memberships is not a subset of your own view.
select wiki_test.expect_eq('a set is refused if one member of it is not granted',
  wiki.may_impersonate_groups(wiki_test.who('erin'),
    array[wiki_test.grp('everyone'), wiki_test.grp('engineering')]), false);

select wiki_test.expect_eq('a person is not a membership',
  wiki.may_impersonate_groups(wiki_test.who('dave'),
    array[wiki_test.who('bob')]), false);

select wiki_test.expect_eq('an empty set is not a membership either',
  wiki.may_impersonate_groups(wiki_test.who('dave'), array[]::uuid[]), false);

------------------------------------------------------------------------------
-- What bob actually sees, for comparison
------------------------------------------------------------------------------

create temp table bob_view (id uuid primary key);
-- The observations are gathered while SET ROLE'd to fswiki_user, which is the
-- only way the RLS being tested is actually in force.
grant insert, select on bob_view to fswiki_user;
select wiki_test.login('bob');
set role fswiki_user;
insert into bob_view select id from wiki.syncable_document;
reset role;

create temp table obs (k text primary key, v text);
create temp table seen (k text not null, id uuid not null);
grant insert, select on obs, seen to fswiki_user;

------------------------------------------------------------------------------
-- Acting as a person
------------------------------------------------------------------------------

begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_subject => wiki_test.who('bob'));
  set role fswiki_user;

  insert into seen select 'as-bob', id from wiki.syncable_document;

  insert into obs values
    ('as_bob_effective',  wiki.current_user_id()::text),
    ('as_bob_actual',     wiki.authenticated_user_id()::text),
    -- The lock, tested through an ordinary client write rather than a contrived
    -- one: this is the statement an editor's save turns into.
    ('as_bob_write',      wiki_test.sqlstate_of(
       $q$insert into wiki.draft (author_id, operation, path, content)
          values (wiki.current_user_id(), 'create', 'root.public.forged', 'not bob')$q$)),
    ('as_bob_logged',     (select count(*) from wiki.impersonation_event
                            where actor_id = wiki_test.who('dave')
                              and subject_id = wiki_test.who('bob'))::text);
  reset role;
commit;

select wiki_test.expect_eq('acting as bob resolves to bob',
  (select v from obs where k = 'as_bob_effective'), wiki_test.who('bob')::text);

select wiki_test.expect_eq('while the caller is still dave underneath',
  (select v from obs where k = 'as_bob_actual'), wiki_test.who('dave')::text);

select wiki_test.expect_eq('dave-as-bob sees exactly what bob sees',
  (select count(*)::int from (
     select id from seen where k = 'as-bob'
     except select id from bob_view) x)
  + (select count(*)::int from (
     select id from bob_view
     except select id from seen where k = 'as-bob') y), 0);

select wiki_test.expect_eq('and it is not an empty view being compared',
  (select count(*)::int > 0 from bob_view), true);

-- 25006: cannot execute INSERT in a read-only transaction. Nothing in draft's
-- policies said no; the transaction did, which is the point. A list of write
-- paths would have to be kept correct forever; this cannot be forgotten.
select wiki_test.expect_eq('an impersonated write is refused by the transaction',
  (select v from obs where k = 'as_bob_write'), '25006');

select wiki_test.expect_eq('and the impersonation was recorded before the lock',
  (select v from obs where k = 'as_bob_logged'), '1');

select wiki_test.expect_eq('filed against dave, never against bob',
  (select count(*)::int from wiki.impersonation_event
    where actor_id = wiki_test.who('bob')), 0);

select wiki_test.expect_eq('nothing was forged into bob''s drafts',
  (select count(*)::int from wiki.draft where path = 'root.public.forged'), 0);

------------------------------------------------------------------------------
-- Acting as a membership
------------------------------------------------------------------------------
--
-- bob is in exactly {everyone, engineering} and has no ACE naming him, so a
-- principal whose only memberships are those two must land on his view exactly.

begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_groups =>
    array[wiki_test.grp('everyone'), wiki_test.grp('engineering')]);
  set role fswiki_user;

  insert into seen select 'as-pair', id from wiki.syncable_document;
  insert into obs values
    ('pair_effective', wiki.current_user_id()::text),
    -- Through RLS, as a client would see it: draft's policy is
    -- author_id = current_user_id(), and a hypothetical worker has no drafts
    -- for the same reason they own nothing -- they are not anybody.
    ('pair_drafts',    (select count(*) from wiki.draft)::text),
    ('pair_logged',    (select count(*) from wiki.impersonation_event
                         where subject_groups is not null)::text);
  reset role;
commit;

-- The measurement the whole design rests on.
select wiki_test.expect_eq('a membership of {everyone, engineering} sees exactly what bob sees',
  (select count(*)::int from (
     select id from seen where k = 'as-pair'
     except select id from bob_view) x)
  + (select count(*)::int from (
     select id from bob_view
     except select id from seen where k = 'as-pair') y), 0);

-- The identity is a function of the group set, not a fresh uuid per request, so
-- the log can be grouped by "acted as this membership" across requests.
select wiki_test.expect_eq('the id is derived from the group set',
  (select v from obs where k = 'pair_effective'),
  wiki.synthetic_principal_id(
    array[wiki_test.grp('everyone'), wiki_test.grp('engineering')])::text);

-- Everything below follows from that id matching no row anywhere, which the
-- forced version nibble makes a property rather than a probability. None of it
-- is checked for in code: ownership, superuser and draft authorship all just
-- fail to match, and that is the correct answer for a person who does not exist.
select wiki_test.expect_eq('the ephemeral user is nobody: no row in principal',
  (select count(*)::int from wiki.principal
    where id = (select v from obs where k = 'pair_effective')::uuid), 0);

select wiki_test.expect_eq('so it cannot be a superuser',
  wiki.is_superuser((select v from obs where k = 'pair_effective')::uuid), false);

select wiki_test.expect_eq('owns nothing, which is right for a hypothetical worker',
  (select count(*)::int from wiki.document
    where owner_id = (select v from obs where k = 'pair_effective')::uuid), 0);

select wiki_test.expect_eq('and has no drafts',
  (select v from obs where k = 'pair_drafts'), '0');

-- gen_random_uuid() is a v4 generator, so its version nibble is always '4'.
-- Forcing '0' is what makes a collision with a real principal impossible rather
-- than merely unlikely.
select wiki_test.expect_eq('the id is of a shape gen_random_uuid cannot produce',
  substring((select v from obs where k = 'pair_effective') from 15 for 1), '0');

select wiki_test.expect_eq('the membership impersonation is on the record too',
  (select v from obs where k = 'pair_logged'), '1');

select wiki_test.expect_eq('recorded as the group set, not as a subject',
  (select count(*)::int from wiki.impersonation_event
    where subject_groups @> array[wiki_test.grp('everyone')]
      and subject_groups @> array[wiki_test.grp('engineering')]), 1);

------------------------------------------------------------------------------
-- Why it is a set and not a group
------------------------------------------------------------------------------
--
-- Naming the group alone is wrong twice over, and the fixtures reproduce both.

begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_groups =>
    array[wiki_test.grp('engineering')]);
  set role fswiki_user;
  insert into seen select 'as-eng', id from wiki.syncable_document;
  reset role;
commit;

-- Under-reports: engineering is in no other group, and nobody is in only one.
select wiki_test.expect_eq('engineering alone misses documents a real engineer has',
  (select count(*)::int > 0 from (
     select id from bob_view
     except select id from seen where k = 'as-eng') x), true);

-- Over-reports, which is the dangerous direction: `deny everyone sync` on
-- secret-plans never reaches a principal that is not in everyone. So the group
-- may mirror a document no human in this wiki may mirror.
select wiki_test.expect_eq('engineering alone may sync secret-plans',
  (select count(*)::int from seen
    where k = 'as-eng' and id = wiki_test.doc('root.engineering.secret-plans')), 1);

select wiki_test.expect_eq('bob, an actual engineer, may not',
  (select count(*)::int from bob_view
    where id = wiki_test.doc('root.engineering.secret-plans')), 0);

select wiki_test.expect_eq('and neither may the membership, because the deny reaches it',
  (select count(*)::int from seen
    where k = 'as-pair' and id = wiki_test.doc('root.engineering.secret-plans')), 0);

------------------------------------------------------------------------------
-- Refusals
------------------------------------------------------------------------------

-- The finding that shaped the feature. PostgREST opens a read-only transaction
-- for GET before the hook runs, so on GET the log cannot be written -- and an
-- impersonation nobody can audit is the abuse the feature invites. Refuse it
-- with a reason rather than letting the insert fail with a bare 25006.
begin;
  set transaction read only;
  select wiki_test.login('dave');
  insert into obs values ('readonly_refusal', wiki_test.sqlstate_of(
    format($q$select wiki.begin_impersonation(p_subject => %L::uuid)$q$,
           wiki_test.who('bob'))));
commit;

select wiki_test.expect_eq('impersonation refuses a transaction that cannot record it',
  (select v from obs where k = 'readonly_refusal'), '42501');

begin;
  select wiki_test.login('frank');
  insert into obs values ('ungranted', wiki_test.sqlstate_of(
    format($q$select wiki.begin_impersonation(p_subject => %L::uuid)$q$,
           wiki_test.who('bob'))));
commit;

select wiki_test.expect_eq('an ungranted impersonation is refused',
  (select v from obs where k = 'ungranted'), '42501');

-- Doing nothing is not an error: a request without the header is an ordinary
-- request, and the hook runs on every one of them.
select wiki_test.expect_eq('no subject and no groups is a no-op, not a failure',
  wiki_test.sqlstate_of($q$select wiki.begin_impersonation()$q$), null::text);

select wiki_test.expect_eq('and it did not lock anything down',
  wiki_test.sqlstate_of(
    $q$insert into wiki.impersonation_grant (actor_id, subject_id)
       select p1.id, p2.id from wiki.principal p1, wiki.principal p2
        where p1.name = 'grace' and p2.name = 'auditors'$q$), null::text);

------------------------------------------------------------------------------
-- One session, not one row per request
------------------------------------------------------------------------------
--
-- The hook runs on every request, and a mount makes a great many: measured, a
-- single `ls` of an impersonated mount is four. A row each would bury the fact
-- anyone cares about under its own volume.

create temp table before_repeat as
  select count(*)::int as n from wiki.impersonation_event;

begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_subject => wiki_test.who('bob'),
                                  p_method => 'POST', p_path => '/rpc/read_document');
commit;
begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_subject => wiki_test.who('bob'),
                                  p_method => 'POST', p_path => '/rpc/list_documents');
commit;

select wiki_test.expect_eq('repeat requests do not add rows',
  (select count(*)::int from wiki.impersonation_event) - (select n from before_repeat), 0);

select wiki_test.expect_eq('they extend the session instead',
  (select requests >= 3 from wiki.impersonation_event
    where subject_id = wiki_test.who('bob')), true);

-- A different subject is a different session, however close together they are.
begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_subject => wiki_test.who('carol'),
                                  p_method => 'POST', p_path => '/rpc/opened-it');
commit;
begin;
  select wiki_test.login('dave');
  select wiki.begin_impersonation(p_subject => wiki_test.who('carol'),
                                  p_method => 'POST', p_path => '/rpc/came-later');
commit;

select wiki_test.expect_eq('a different subject opens its own session',
  (select count(*)::int from wiki.impersonation_event
    where subject_id = wiki_test.who('carol')), 1);

-- The opening request is what the row names. A session that renamed itself to
-- whatever arrived last would answer a question nobody asked, and would make
-- the timestamp and the path describe different requests.
select wiki_test.expect_eq('and it names the request that opened it, not the last one',
  (select path from wiki.impersonation_event where subject_id = wiki_test.who('carol')),
  '/rpc/opened-it');

select wiki_test.expect_eq('and does not disturb the other one',
  (select count(*)::int from wiki.impersonation_event
    where subject_id = wiki_test.who('bob')), 1);

------------------------------------------------------------------------------
-- The trail
------------------------------------------------------------------------------

select wiki_test.expect_eq('an actor reads their own impersonation log',
  (select count(*)::int > 0 from wiki.impersonation_event), true);

reset role;
select wiki_test.login('dave');
set role fswiki_user;
select wiki_test.expect_eq('dave sees his',
  (select count(*)::int > 0 from wiki.impersonation_event), true);
reset role;

select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect_eq('bob does not, though he was the subject',
  (select count(*)::int from wiki.impersonation_event), 0);
reset role;

delete from wiki.impersonation_grant;
delete from wiki.impersonation_event;
