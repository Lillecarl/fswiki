-- The `public` group: pages readable without an account.
--
-- Two layers have to agree before an anonymous browser sees a page, and this
-- file tests exactly one of them. The ACL layer is here: who a caller resolves
-- to, and which rows RLS then admits. The *grant* layer -- what the
-- fswiki_anon database role may select and execute -- is deliberately still
-- shut, and the last block asserts that it is, so that this file says plainly
-- which half of the feature exists.
--
-- The blocks below drop to fswiki_user rather than fswiki_anon for that
-- reason: fswiki_anon has no table privileges at all, so a query as that role
-- fails on the grant before RLS has an opinion, and would tell us nothing
-- about the ACL.

------------------------------------------------------------------------------
-- The group itself
------------------------------------------------------------------------------

select wiki_test.expect_eq('public is a built-in group',
  (select count(*)::int from wiki.principal
    where kind = 'group' and name = 'public'), 1);

-- Nobody is a member, and nothing should ever add one: membership is what
-- effective_principals() supplies, not what group_member holds.
select wiki_test.expect_eq('public has no members',
  (select count(*)::int from wiki.group_member gm
     join wiki.principal p on p.id = gm.group_id
    where p.kind = 'group' and p.name = 'public'), 0);

------------------------------------------------------------------------------
-- A page granted to public
------------------------------------------------------------------------------

insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
select d.id, 'notices', false, 'Notices',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d where d.path = 'root'::ltree;

insert into wiki.document_version (document_id, version, path, content, message, author_id)
select d.id, 1, d.path, 'Contents of ' || d.title, 'initial',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d where d.path = 'root.notices'::ltree;

insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id,
       (select p.id from wiki.principal p where p.kind = 'group' and p.name = 'public'),
       (select r.id from wiki.role r where r.name = 'reader'),
       'allow'
  from wiki.document d where d.path = 'root.notices'::ltree;

------------------------------------------------------------------------------
-- Everyone is in public, logged in or not
------------------------------------------------------------------------------

-- erin has no groups and no ACEs. 020_rls_test.sql asserts she sees literally
-- nothing; the only thing that has changed is the grant to public, so anything
-- she can see now, she can see *because* of it.
select wiki_test.login('erin');
set role fswiki_user;

select wiki_test.expect_eq('erin: still resolves to a real principal',
  (select wiki.current_user_id() is not null), true);
select wiki_test.expect_eq('erin: sees the public page and the route to it',
  (select array_agg(path::text order by path) from wiki.document),
  array['root', 'root.notices']);
select wiki_test.expect_eq('erin: may read its content',
  (select count(*)::int from wiki.document_version), 1);

reset role;

-- And a user who already had access keeps it, with public on top rather than
-- instead: bob reads his own tree as well as the public page.
select wiki_test.login('bob');
set role fswiki_user;

select wiki_test.expect_eq('bob: public is added to what he already had',
  (select count(*)::int from wiki.document where path = 'root.notices'::ltree), 1);
select wiki_test.expect_eq('bob: and he still sees more than erin does',
  (select count(*)::int > 2 from wiki.document), true);

reset role;

------------------------------------------------------------------------------
-- No account at all
------------------------------------------------------------------------------

select set_config('request.jwt.claims', '', false);

select wiki_test.expect_eq('anonymous: resolves to no user',
  (select wiki.current_user_id() is null), true);
select wiki_test.expect_eq('anonymous: resolves to public and nothing else',
  (select array_agg(ep.principal_id) from wiki.effective_principals(null) ep),
  array[(select p.id from wiki.principal p
          where p.kind = 'group' and p.name = 'public')]);

set role fswiki_user;

select wiki_test.expect_eq('anonymous: sees the public page',
  (select array_agg(path::text order by path) from wiki.document),
  array['root', 'root.notices']);

-- The reason this is a group and not an anonymous user account. These policies
-- are phrased `current_user_id() is not null`, and a user account would have
-- turned every one of them on for the whole internet at once.
select wiki_test.expect_eq('anonymous: sees no principals',
  (select count(*)::int from wiki.principal), 0);
select wiki_test.expect_eq('anonymous: sees no user accounts',
  (select count(*)::int from wiki.user_account), 0);
select wiki_test.expect_eq('anonymous: sees no group memberships',
  (select count(*)::int from wiki.group_member), 0);
select wiki_test.expect_eq('anonymous: sees no drafts',
  (select count(*)::int from wiki.draft), 0);

reset role;

------------------------------------------------------------------------------
-- The half that is not built yet
------------------------------------------------------------------------------
--
-- A real anonymous request arrives as fswiki_anon, which has no table
-- privileges and no EXECUTE on the ACL engine -- see 040_grants_test.sql,
-- which asserts that deliberately: those functions take a principal as an
-- argument, so an unauthenticated caller who could execute them could ask
-- what *anyone* may read. Opening this up needs an answer to that, and these
-- assertions are here to fail the day someone opens it without one.

select wiki_test.expect_eq('anon role still cannot select documents',
  has_table_privilege('fswiki_anon', 'wiki.document', 'select'), false);
select wiki_test.expect_eq('anon role still cannot execute the ACL walk',
  has_function_privilege('fswiki_anon',
    'wiki.can(ltree, boolean, uuid, wiki.capability, uuid)', 'execute'), false);
