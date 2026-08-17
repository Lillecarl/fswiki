-- ACL and RLS behaviour tests.
--
-- Each block logs in as a user, drops to fswiki_user, and asserts what that
-- user can see and do. Anything asserted through wiki.has_capability() is the
-- ACL; anything asserted by counting rows is RLS actually enforcing it.

------------------------------------------------------------------------------
-- erin: no groups, no ACEs. Should see literally nothing.
------------------------------------------------------------------------------
select wiki_test.login('erin');
set role fswiki_user;

select wiki_test.expect_eq('erin: resolves to a principal',
  (select wiki.current_user_id() is not null), true);
select wiki_test.expect_eq('erin: sees no documents',
  (select count(*)::int from wiki.document), 0);
select wiki_test.expect_eq('erin: sees no versions',
  (select count(*)::int from wiki.document_version), 0);

reset role;

select wiki_test.expect_eq('erin: no capabilities anywhere',
  (select wiki.capabilities_at(wiki_test.doc('root.public'), wiki_test.who('erin'))),
  '{}'::wiki.capability[]);

------------------------------------------------------------------------------
-- Inheritance and traversal, seen through dave (member of `everyone` only).
------------------------------------------------------------------------------
select wiki_test.login('dave');
set role fswiki_user;

-- Folders he cannot read still appear when they contain something he can:
-- root and engineering are traversal-only, io-test is inherit-only.
select wiki_test.expect_eq('dave: sees the traversable tree and nothing else',
  (select array_agg(path::text order by path) from wiki.document),
  array['root',
        'root.engineering',
        'root.engineering.private',
        'root.engineering.private.memo',
        'root.io-test',
        'root.io-test.child',
        'root.public',
        'root.public.archive',
        'root.public.archive.old-post',
        'root.public.welcome']);

select wiki_test.expect_eq('dave: reads welcome by inheritance from public',
  (select wiki.has_capability(wiki_test.doc('root.public.welcome'), 'read')), true);
select wiki_test.expect_eq('dave: inheritance reaches two levels down',
  (select wiki.has_capability(wiki_test.doc('root.public.archive.old-post'), 'read')), true);
select wiki_test.expect_eq('dave: cannot write anything in public',
  (select wiki.has_capability(wiki_test.doc('root.public.welcome'), 'write')), false);
select wiki_test.expect_eq('dave: root is traversal-visible, not readable',
  (select wiki.has_capability(wiki_test.doc('root'), 'read')), false);
select wiki_test.expect_eq('dave: engineering is traversal-visible only',
  (select wiki.has_capability(wiki_test.doc('root.engineering'), 'read')), false);
select wiki_test.expect_eq('dave: sees versions only for readable documents',
  (select count(*)::int from wiki.document_version), 4);
select wiki_test.expect_denied('dave: cannot create under public',
  $sql$insert into wiki.document (parent_id, slug, title)
       select id, 'sneaky', 'Sneaky' from wiki.document where path = 'root.public'$sql$);

-- inherit_only (IO): the ACE skips the document it is attached to.
select wiki_test.expect_eq('dave: inherit_only ACE does not apply to its own node',
  (select wiki.has_capability(wiki_test.doc('root.io-test'), 'read')), false);
select wiki_test.expect_eq('dave: inherit_only ACE does apply to children',
  (select wiki.has_capability(wiki_test.doc('root.io-test.child'), 'read')), true);

reset role;

------------------------------------------------------------------------------
-- no_propagate (NP): immediate children only. grace is an auditor.
------------------------------------------------------------------------------
select wiki_test.login('grace');
set role fswiki_user;

select wiki_test.expect_eq('grace: NP ACE applies to the node it sits on',
  (select wiki.has_capability(wiki_test.doc('root.public'), 'write')), true);
select wiki_test.expect_eq('grace: NP ACE reaches immediate children',
  (select wiki.has_capability(wiki_test.doc('root.public.welcome'), 'write')), true);
select wiki_test.expect_eq('grace: NP ACE stops at depth 2',
  (select wiki.has_capability(wiki_test.doc('root.public.archive.old-post'), 'write')), false);
select wiki_test.expect_eq('grace: but still reads depth 2 via the everyone ACE',
  (select wiki.has_capability(wiki_test.doc('root.public.archive.old-post'), 'read')), true);

reset role;

------------------------------------------------------------------------------
-- bob: engineering editor. Role inheritance, and the inheritance_blocked wall.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

select wiki_test.expect_eq('bob: editor inherits write from the role hierarchy',
  (select wiki.has_capability(wiki_test.doc('root.engineering.onboarding'), 'write')), true);
select wiki_test.expect_eq('bob: editor inherits create',
  (select wiki.has_capability(wiki_test.doc('root.engineering.onboarding'), 'create')), true);
select wiki_test.expect_eq('bob: editor does not get delete',
  (select wiki.has_capability(wiki_test.doc('root.engineering.onboarding'), 'delete')), false);
select wiki_test.expect_eq('bob: capabilities_at reports the whole set',
  (select wiki.capabilities_at(wiki_test.doc('root.engineering.onboarding'))),
  array['read', 'sync', 'write', 'create']::wiki.capability[]);
-- `editor` never mentions read; it arrives because write requires it.
select wiki_test.expect_eq('bob: write implies read',
  (select wiki.has_capability(wiki_test.doc('root.engineering.onboarding'), 'read')), true);
select wiki_test.expect_eq('bob: reads secret-plans (not a contractor)',
  (select wiki.has_capability(wiki_test.doc('root.engineering.secret-plans'), 'read')), true);
select wiki_test.expect_eq('bob: engineering rights do not leak into public',
  (select wiki.has_capability(wiki_test.doc('root.public.welcome'), 'write')), false);

-- sync: denying it leaves the document readable but keeps it off local disks.
-- Nothing requires `sync`, so the upward closure stops at itself and read
-- survives — the asymmetry that makes a browser-only document possible.
select wiki_test.expect_eq('bob: secret-plans stays readable',
  (select wiki.has_capability(wiki_test.doc('root.engineering.secret-plans'), 'read')), true);
select wiki_test.expect_eq('bob: but is not syncable',
  (select wiki.has_capability(wiki_test.doc('root.engineering.secret-plans'), 'sync')), false);
select wiki_test.expect_eq('bob: it is present in the readable tree',
  (select count(*)::int from wiki.document
    where path = 'root.engineering.secret-plans'), 1);
select wiki_test.expect_eq('bob: and absent from the syncable tree',
  (select count(*)::int from wiki.syncable_document
    where path = 'root.engineering.secret-plans'), 0);
select wiki_test.expect_eq('bob: its folder is still syncable via its siblings',
  (select count(*)::int from wiki.syncable_document
    where path = 'root.engineering'), 1);
select wiki_test.expect_eq('bob: syncable is a strict subset of readable here',
  (select (select count(*) from wiki.syncable_document)
        < (select count(*) from wiki.document)), true);

-- inheritance_blocked: `private` refuses ACEs from above, so bob's editor
-- rights stop at the wall and only private's own everyone/reader ACE applies.
select wiki_test.expect_eq('bob: blocked inheritance stops his editor rights',
  (select wiki.has_capability(wiki_test.doc('root.engineering.private'), 'write')), false);
select wiki_test.expect_eq('bob: but the local ACE on private still grants read',
  (select wiki.has_capability(wiki_test.doc('root.engineering.private'), 'read')), true);
select wiki_test.expect_eq('bob: the wall applies below it too',
  (select wiki.has_capability(wiki_test.doc('root.engineering.private.memo'), 'write')), false);
select wiki_test.expect_eq('bob: and local ACEs still inherit past the wall',
  (select wiki.has_capability(wiki_test.doc('root.engineering.private.memo'), 'read')), true);

reset role;

------------------------------------------------------------------------------
-- carol: engineering AND contractors. The precedence rules, in order.
------------------------------------------------------------------------------
select wiki_test.login('carol');
set role fswiki_user;

-- Same distance (both ACEs sit on `engineering`): deny is consulted first.
select wiki_test.expect_eq('carol: deny beats allow at equal distance',
  (select wiki.has_capability(wiki_test.doc('root.engineering'), 'read')), false);

-- The deny carries `reader`, but capabilities are hierarchical: write requires
-- read, so denying read takes write with it. Without the upward closure Carol
-- would be able to write a document she cannot read.
select wiki_test.expect_eq('carol: denying read denies write too',
  (select wiki.has_capability(wiki_test.doc('root.engineering'), 'write')), false);
select wiki_test.expect_eq('carol: and everything else that needs read',
  (select wiki.capabilities_at(wiki_test.doc('root.engineering'))),
  '{}'::wiki.capability[]);

-- The headline case: an explicit ACE on the document outranks an inherited one,
-- so a single page can be carved out of a denied subtree.
select wiki_test.expect_eq('carol: explicit allow outranks inherited deny',
  (select wiki.has_capability(wiki_test.doc('root.engineering.onboarding'), 'read')), true);

-- No carve-out here, so the inherited deny still stands.
select wiki_test.expect_eq('carol: inherited deny still applies to siblings',
  (select wiki.has_capability(wiki_test.doc('root.engineering.secret-plans'), 'read')), false);
select wiki_test.expect_eq('carol: secret-plans is absent, not forbidden',
  (select count(*)::int from wiki.document
    where path = 'root.engineering.secret-plans'), 0);
select wiki_test.expect_eq('carol: onboarding is present',
  (select count(*)::int from wiki.document
    where path = 'root.engineering.onboarding'), 1);

reset role;

select wiki_test.expect_eq('explain_acl names the ACE that won',
  (select source_path::text from wiki.explain_acl(
     wiki_test.doc('root.engineering.onboarding'), wiki_test.who('carol'))
    where capability = 'read'),
  'root.engineering.onboarding');
select wiki_test.expect_eq('explain_acl marks the winner as explicit',
  (select inherited from wiki.explain_acl(
     wiki_test.doc('root.engineering.onboarding'), wiki_test.who('carol'))
    where capability = 'read'),
  false);
select wiki_test.expect_eq('explain_acl reports an unmatched capability',
  (select verdict from wiki.explain_acl(
     wiki_test.doc('root.public.welcome'), wiki_test.who('dave'))
    where capability = 'administer'),
  'deny (no matching ACE)');

------------------------------------------------------------------------------
-- Owner lockout protection.
------------------------------------------------------------------------------
select wiki_test.login('dave');
set role fswiki_user;

select wiki_test.expect_eq('dave: denied every capability on the document he owns',
  (select wiki.has_capability(wiki_test.doc('root.locked'), 'read')), false);
select wiki_test.expect_eq('dave: owner keeps grant despite a full deny',
  (select wiki.has_capability(wiki_test.doc('root.locked'), 'grant')), true);
select wiki_test.expect_eq('dave: owner cannot write it either, only re-ACL it',
  (select wiki.has_capability(wiki_test.doc('root.locked'), 'write')), false);
-- ...and the escape hatch actually works end to end: he can delete the ACE that
-- locked him out, because ace_delete keys off 'grant'.
create temporary table locked_ace_removed (id uuid);
with gone as (
  delete from wiki.ace where document_id = wiki_test.doc('root.locked') returning id
)
insert into locked_ace_removed select id from gone;

select wiki_test.expect_eq('dave: can drop the offending ACE',
  (select count(*)::int from locked_ace_removed), 1);
select wiki_test.expect_eq('dave: still cannot read it afterwards (no allow ACE)',
  (select wiki.has_capability(wiki_test.doc('root.locked'), 'read')), false);

reset role;

------------------------------------------------------------------------------
-- Superuser.
------------------------------------------------------------------------------
select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_eq('alice: superuser sees every document',
  (select count(*)::int from wiki.document), 13);
select wiki_test.expect_eq('alice: superuser has administer anywhere',
  (select wiki.has_capability(wiki_test.doc('root.engineering.private'), 'administer')), true);

reset role;

------------------------------------------------------------------------------
-- Drafts are private to their author.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'Bob was here', 1
  from wiki.document d where d.path = 'root.engineering.onboarding';

select wiki_test.expect_eq('bob: sees his own draft',
  (select count(*)::int from wiki.draft), 1);
reset role;

select wiki_test.login('carol');
set role fswiki_user;
select wiki_test.expect_eq('carol: cannot see bob''s draft',
  (select count(*)::int from wiki.draft), 0);
-- Targets a path bob has no draft on, so a unique-constraint failure cannot
-- stand in for the policy rejection we are actually testing.
select wiki_test.expect_denied('carol: cannot forge a draft as bob',
  $sql$insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
       select p.id, 'update', d.id, d.path, 'forged', 1
         from wiki.document d, wiki.principal p
        where d.path = 'root.public.welcome' and p.kind = 'user' and p.name = 'bob'$sql$);
reset role;

------------------------------------------------------------------------------
-- Delegation: only a 'grant' holder may touch an ACL.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect_eq('bob: editor does not hold grant',
  (select wiki.has_capability(wiki_test.doc('root.engineering'), 'grant')), false);
select wiki_test.expect_denied('bob: cannot write himself a new ACE',
  $sql$insert into wiki.ace (document_id, principal_id, role_id, ace_type)
       select d.id, wiki.current_user_id(), r.id, 'allow'
         from wiki.document d
        cross join lateral (select wiki.role_id('owner') as id) r
        where d.path = 'root.engineering'$sql$);
reset role;

------------------------------------------------------------------------------
-- The capability lattice: delete sits low, purge sits alone at the top.
------------------------------------------------------------------------------
select wiki_test.login('frank');
set role fswiki_user;

select wiki_test.expect_eq('frank: delete implies read',
  (select wiki.has_capability(wiki_test.doc('root.io-test'), 'read')), true);
select wiki_test.expect_eq('frank: delete does not imply write',
  (select wiki.has_capability(wiki_test.doc('root.io-test'), 'write')), false);
select wiki_test.expect_eq('frank: nor sync',
  (select wiki.has_capability(wiki_test.doc('root.io-test'), 'sync')), false);
select wiki_test.expect_eq('frank: retiring is not destroying',
  (select wiki.has_capability(wiki_test.doc('root.io-test'), 'purge')), false);
-- RLS filters DELETE, it does not raise on it: a row the DELETE policy rejects
-- is simply not among the rows deleted, and the statement reports success. Any
-- client that treats "no error" as "it worked" will silently do nothing here,
-- so the CLI has to check the row count.
delete from wiki.document where slug = 'child';

select wiki_test.expect_eq('frank: the purge is filtered out and the row survives',
  (select count(*)::int from wiki.document where slug = 'child'), 1);

reset role;

select wiki_test.expect_eq('owner role stops short of purge',
  (select wiki.ace_covers(
     wiki.role_id('owner'), 'purge', 'allow')), false);
select wiki_test.expect_eq('purge still implies delete and administer',
  (select array_agg(capability order by capability)
     from wiki.capability_downward('purge')),
  array['read', 'delete', 'grant', 'administer', 'purge']::wiki.capability[]);

------------------------------------------------------------------------------
-- Temporal versioning (as owner; these are constraints, not policies).
------------------------------------------------------------------------------

-- Two open-ended intervals always overlap, so the exclusion constraint catches
-- this before the one-live-revision index does.
select wiki_test.expect_rejected('a document cannot have two live revisions',
  $sql$insert into wiki.document_version (document_id, version, path, content, author_id)
       select d.id, 2, d.path, 'second head', wiki_test.who('alice')
         from wiki.document d where d.path = 'root.public.welcome'$sql$,
  '23P01');

-- Publishing properly: close the open interval, then open the next.
update wiki.document_version
   set valid = tstzrange(lower(valid), now())
 where document_id = wiki_test.doc('root.public.welcome') and upper_inf(valid);

insert into wiki.document_version (document_id, version, path, content, message, author_id)
select d.id, 2, d.path, 'Second revision', 'edit', wiki_test.who('alice')
  from wiki.document d where d.path = 'root.public.welcome';

select wiki_test.expect_eq('current_document follows the open interval',
  (select content from wiki.current_document where path = 'root.public.welcome'),
  'Second revision');
select wiki_test.expect_eq('history keeps the superseded revision',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.public.welcome')), 2);
select wiki_test.expect_eq('point-in-time reads see the old revision',
  (select content from wiki.document_as_of(
     (select lower(valid) + interval '1 microsecond'
        from wiki.document_version
       where document_id = wiki_test.doc('root.public.welcome') and version = 1))
    where path = 'root.public.welcome'),
  'Contents of Welcome');

-- Retirement is a tombstone, not a row removal.
update wiki.document_version
   set valid = tstzrange(lower(valid), now())
 where document_id = wiki_test.doc('root.public.archive.old-post') and upper_inf(valid);

insert into wiki.document_version (document_id, version, path, is_tombstone, message, author_id)
select d.id, 2, d.path, true, 'retired', wiki_test.who('alice')
  from wiki.document d where d.path = 'root.public.archive.old-post';

select wiki_test.expect_eq('a tombstone drops the document from the current view',
  (select count(*)::int from wiki.current_document
    where path = 'root.public.archive.old-post'), 0);
select wiki_test.expect_eq('but the row and its history survive',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.public.archive.old-post')), 2);
select wiki_test.expect_eq('and it is still there before it was retired',
  (select count(*)::int from wiki.document_as_of(
     (select lower(valid) + interval '1 microsecond'
        from wiki.document_version
       where document_id = wiki_test.doc('root.public.archive.old-post') and version = 1))
    where path = 'root.public.archive.old-post'), 1);

select wiki_test.expect_rejected('a tombstone may not carry content',
  $sql$insert into wiki.document_version
         (document_id, version, path, content, is_tombstone, author_id)
       select d.id, 3, d.path, 'oops', true, wiki_test.who('alice')
         from wiki.document d where d.path = 'root.public.archive.old-post'$sql$,
  '23514');

-- A bounded interval straddling the live one. A range wholly in the past would
-- be accepted, and correctly so: it fills a gap rather than overlapping.
select wiki_test.expect_rejected('overlapping validity intervals are rejected',
  $sql$insert into wiki.document_version (document_id, version, path, content, valid, author_id)
       select d.id, 9, d.path, 'straddles the live revision',
              tstzrange(now() - interval '1 hour', now() + interval '1 hour'),
              wiki_test.who('alice')
         from wiki.document d where d.path = 'root.engineering.onboarding'$sql$,
  '23P01');

------------------------------------------------------------------------------
-- Structural invariants (checked as owner; these are not RLS).
------------------------------------------------------------------------------
select wiki_test.expect_rejected('group cycles are rejected',
  $sql$insert into wiki.group_member (group_id, member_id)
       select e.id, c.id from wiki.principal e, wiki.principal c
        where e.name = 'engineering' and c.name = 'contractors';
       insert into wiki.group_member (group_id, member_id)
       select c.id, e.id from wiki.principal e, wiki.principal c
        where e.name = 'engineering' and c.name = 'contractors'$sql$);

select wiki_test.expect_rejected('role inheritance cycles are rejected',
  $sql$insert into wiki.role_inherits (role_id, inherits_role_id)
       select p.id, c.id from wiki.role p, wiki.role c
        where p.name = 'reader' and c.name = 'owner'$sql$);

select wiki_test.expect_rejected('a document cannot be moved beneath itself',
  $sql$update wiki.document
          set parent_id = (select id from wiki.document where path = 'root.engineering.private')
        where path = 'root.engineering'$sql$);

-- Moving a subtree rewrites descendant paths...
update wiki.document set slug = 'eng' where path = 'root.engineering';

select wiki_test.expect_eq('subtree move rewrites descendant paths',
  (select array_agg(path::text order by path) from wiki.document where path <@ 'root.eng'),
  array['root.eng', 'root.eng.onboarding', 'root.eng.private',
        'root.eng.private.memo', 'root.eng.secret-plans']);

-- ...and because ACEs hang off document ids rather than paths, permissions
-- survive the move untouched. This is the payoff for per-object ACLs over a
-- path-scoped grant table.
select wiki_test.expect_eq('ACEs survive a subtree move',
  (select wiki.has_capability(wiki_test.doc('root.eng.onboarding'), 'read', wiki_test.who('carol'))),
  true);
select wiki_test.expect_eq('inherited denies survive a subtree move too',
  (select wiki.has_capability(wiki_test.doc('root.eng.secret-plans'), 'read', wiki_test.who('carol'))),
  false);

update wiki.document set slug = 'engineering' where path = 'root.eng';
