-- wiki.push(): publishing drafts atomically.
--
-- Runs after 020, so it inherits that file's state: welcome is at revision 2,
-- old-post is retired, and bob has an unpublished draft on onboarding.

create or replace function wiki_test.live_version(p_path text)
returns integer
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select v.version
    from wiki.document_version v
    join wiki.document d on d.id = v.document_id
   where d.path = p_path::ltree and upper_inf(v.valid);
$$;

create or replace function wiki_test.live_content(p_path text)
returns text
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select v.content
    from wiki.document_version v
    join wiki.document d on d.id = v.document_id
   where d.path = p_path::ltree and upper_inf(v.valid);
$$;

grant execute on all functions in schema wiki_test to fswiki_user;

------------------------------------------------------------------------------
-- The happy path: bob publishes the draft 020 left behind.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

create temporary table push_ok as select * from wiki.push('publishing my edit');

select wiki_test.expect_eq('push: the draft is published',
  (select status::text from push_ok), 'published');
select wiki_test.expect_eq('push: the revision number is bumped',
  (select version from push_ok), 2);
select wiki_test.expect_eq('push: the content is live',
  (select content from wiki.current_document where path = 'root.engineering.onboarding'),
  'Bob was here');
select wiki_test.expect_eq('push: history keeps the previous revision',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.onboarding')), 2);
select wiki_test.expect_eq('push: the draft is consumed',
  (select count(*)::int from wiki.draft), 0);

reset role;
select wiki_test.expect_eq('push: the superseded revision was closed',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.onboarding')
      and version = 1 and not upper_inf(valid)), 1);

------------------------------------------------------------------------------
-- Conflict: the draft was based on a revision the server has moved past.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'edited from a stale copy', 1
  from wiki.document d where d.path = 'root.engineering.onboarding';

create temporary table push_conflict as select * from wiki.push();

select wiki_test.expect_eq('push: a stale base version is a conflict',
  (select status::text from push_conflict), 'conflict');
select wiki_test.expect_eq('push: the conflict reports the server revision',
  (select server_version from push_conflict), 2);
select wiki_test.expect_eq('push: and hands back the server content to merge from',
  (select server_content from push_conflict), 'Bob was here');

-- All three sides of a three-way merge come back from the one call. 'mine' is
-- the draft the client already holds; 'theirs' is server_content above; and
-- base_content is revision 1, the ancestor, which is no longer live and which
-- the client may never have had.
select wiki_test.expect_eq('push: the conflict hands back the base revision too',
  (select base_content from push_conflict), 'Contents of Onboarding');
select wiki_test.expect_eq('push: base and server content are genuinely different',
  (select base_content is distinct from server_content from push_conflict), true);
select wiki_test.expect_eq('push: nothing was written',
  (select content from wiki.current_document where path = 'root.engineering.onboarding'),
  'Bob was here');
select wiki_test.expect_eq('push: the draft survives for the client to retry',
  (select count(*)::int from wiki.draft), 1);

-- Rebasing onto the server revision makes the same draft publishable.
update wiki.draft set base_version = 2, content = 'merged by bob';
create temporary table push_rebased as select * from wiki.push();

select wiki_test.expect_eq('push: rebasing resolves the conflict',
  (select status::text from push_rebased), 'published');
select wiki_test.expect_eq('push: now at revision 3',
  (select version from push_rebased), 3);

reset role;

------------------------------------------------------------------------------
-- Permissions are enforced inside the function, not by RLS.
------------------------------------------------------------------------------
select wiki_test.login('carol');
set role fswiki_user;

-- Carol may read onboarding through her explicit ACE, but the inherited deny
-- still takes write, so publishing must be refused.
select wiki_test.expect_eq('carol: reads onboarding',
  (select wiki.has_capability(wiki_test.doc('root.engineering.onboarding'), 'read')), true);

insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'carol was here', 3
  from wiki.document d where d.path = 'root.engineering.onboarding';

create temporary table push_forbidden as select * from wiki.push();

select wiki_test.expect_eq('push: a draft she may not write is forbidden',
  (select status::text from push_forbidden), 'forbidden');
select wiki_test.expect_eq('push: and the document is untouched',
  (select wiki_test.live_content('root.engineering.onboarding')), 'merged by bob');

delete from wiki.draft;
reset role;

------------------------------------------------------------------------------
-- Creating documents, including the folders on the way.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

insert into wiki.draft (author_id, operation, path, content)
values (wiki.current_user_id(), 'create', 'root.engineering.guides.testing', 'How to test');

create temporary table push_create as select * from wiki.push('new guide');

select wiki_test.expect_eq('push: a create is published at revision 1',
  (select status::text || ':' || version from push_create), 'published:1');
select wiki_test.expect_eq('push: the intermediate folder was created',
  (select is_folder from wiki.document where path = 'root.engineering.guides'), true);
select wiki_test.expect_eq('push: the new document is readable',
  (select content from wiki.current_document where path = 'root.engineering.guides.testing'),
  'How to test');
-- The new folder has no ACE of its own; bob reaches it purely by inheritance
-- from the engineering ACE, which is the point of path-based inheritance. He
-- also gets 'grant' on it, because push makes the creator the owner and an
-- owner always keeps the right to fix its ACL — the Windows creator-owner rule.
select wiki_test.expect_eq('push: the new document inherits the folder ACL',
  (select wiki.capabilities_at(wiki_test.doc('root.engineering.guides.testing'))),
  array['read', 'sync', 'write', 'create', 'grant']::wiki.capability[]);
select wiki_test.expect_eq('push: the creator owns what they created',
  (select owner_id from wiki.document where path = 'root.engineering.guides.testing'),
  wiki_test.who('bob'));

-- INSERT ... RETURNING must survive the SELECT policy on the row it just made.
-- It only does because the document policies resolve the ACL from the new row's
-- own columns rather than looking it up by id, which a STABLE function cannot
-- do mid-statement. PostgREST adds RETURNING to every insert, so this is the
-- shape every client write actually takes.
create temporary table returning_check (id uuid);
with ins as (
  insert into wiki.document (parent_id, slug, is_folder, title)
  select id, 'returning-probe', false, 'Probe'
    from wiki.document where path = 'root.engineering'
  returning id
)
insert into returning_check select id from ins;
select wiki_test.expect_eq('insert ... returning passes its own select policy',
  (select count(*)::int from returning_check), 1);

-- And the WITH CHECK on update must judge the *new* path, not the old one:
-- otherwise re-parenting into a subtree you have no rights over slips through.
select wiki_test.expect_denied('re-parenting into a forbidden subtree is refused',
  $sql$update wiki.document
          set parent_id = (select id from wiki.document where path = 'root.public')
        where path = 'root.engineering.returning-probe'$sql$);

-- Creating where one already exists is a conflict, not a silent overwrite.
insert into wiki.draft (author_id, operation, path, content)
values (wiki.current_user_id(), 'create', 'root.engineering.guides.testing', 'clobber');
create temporary table push_clobber as select * from wiki.push();
select wiki_test.expect_eq('push: creating over an existing path conflicts',
  (select status::text from push_clobber), 'conflict');
-- A create never descended from anything, so there is no ancestor to merge
-- against and the client must be told that rather than shown an empty string.
select wiki_test.expect_eq('push: a create collision has no base to merge from',
  (select base_content is null from push_clobber), true);
delete from wiki.draft;

reset role;

-- Someone without 'create' on the target folder is refused.
select wiki_test.login('dave');
set role fswiki_user;

insert into wiki.draft (author_id, operation, path, content)
values (wiki.current_user_id(), 'create', 'root.engineering.guides.sneaky', 'not allowed');

create temporary table push_nocreate as select * from wiki.push();
select wiki_test.expect_eq('push: creating without the capability is forbidden',
  (select status::text from push_nocreate), 'forbidden');

reset role;
select wiki_test.expect_eq('push: and no document was created',
  (select count(*)::int from wiki.document where slug = 'sneaky'), 0);

set role fswiki_user;
delete from wiki.draft;
reset role;

------------------------------------------------------------------------------
-- Retiring: delete without write, because the lattice allows it.
------------------------------------------------------------------------------
select wiki_test.login('frank');
set role fswiki_user;

select wiki_test.expect_eq('frank: holds delete but not write on child',
  (select wiki.has_capability(wiki_test.doc('root.io-test.child'), 'delete')
      and not wiki.has_capability(wiki_test.doc('root.io-test.child'), 'write')), true);

insert into wiki.draft (author_id, operation, document_id, path, base_version)
select wiki.current_user_id(), 'delete', d.id, d.path, 1
  from wiki.document d where d.path = 'root.io-test.child';

create temporary table push_delete as select * from wiki.push('retired');

select wiki_test.expect_eq('push: a retirement publishes as a tombstone revision',
  (select status::text || ':' || version from push_delete), 'published:2');

reset role;
select wiki_test.expect_eq('push: the document drops out of the current view',
  (select count(*)::int from wiki.current_document where path = 'root.io-test.child'), 0);
select wiki_test.expect_eq('push: but the row and its history remain',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.io-test.child')), 2);
select wiki_test.expect_eq('push: retiring twice is invalid, not a second tombstone',
  (select is_tombstone from wiki.document_version
    where document_id = wiki_test.doc('root.io-test.child') and upper_inf(valid)), true);

------------------------------------------------------------------------------
-- Moving: the rename is recorded as a revision, and ACEs follow the document.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

insert into wiki.draft (author_id, operation, document_id, path, base_version)
select wiki.current_user_id(), 'move', d.id, 'root.engineering.guides.onboarding'::ltree, 3
  from wiki.document d where d.path = 'root.engineering.onboarding';

create temporary table push_move as select * from wiki.push('reorganising');

select wiki_test.expect_eq('push: the move is published',
  (select status::text from push_move), 'published');
select wiki_test.expect_eq('push: the document sits at its new path',
  (select count(*)::int from wiki.current_document
    where path = 'root.engineering.guides.onboarding'), 1);
select wiki_test.expect_eq('push: the content came along',
  (select content from wiki.current_document
    where path = 'root.engineering.guides.onboarding'), 'merged by bob');

reset role;
select wiki_test.expect_eq('push: history records where it used to live',
  (select array_agg(path::text order by version) from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.onboarding')),
  array['root.engineering.onboarding',
        'root.engineering.onboarding',
        'root.engineering.onboarding',
        'root.engineering.guides.onboarding']);
-- Carol's carve-out ACE was attached to the document, so it survived the move.
select wiki_test.expect_eq('push: explicit ACEs follow the document across a move',
  (select wiki.has_capability(wiki_test.doc('root.engineering.guides.onboarding'),
                              'read', wiki_test.who('carol'))), true);

------------------------------------------------------------------------------
-- All or nothing: one bad entry rejects the whole changeset.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'this one is fine',
       wiki_test.live_version('root.engineering.guides.onboarding')
  from wiki.document d where d.path = 'root.engineering.guides.onboarding';

-- A base version the server has never had, so this entry cannot apply.
insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'this one is stale', 99
  from wiki.document d where d.path = 'root.engineering.secret-plans';

create temporary table push_mixed as select * from wiki.push();

select wiki_test.expect_eq('push: the changeset reports both entries',
  (select count(*)::int from push_mixed), 2);
select wiki_test.expect_eq('push: one entry conflicts',
  (select count(*)::int from push_mixed where status = 'conflict'), 1);
select wiki_test.expect_eq('push: the valid entry was NOT applied',
  (select content from wiki.current_document
    where path = 'root.engineering.guides.onboarding'), 'merged by bob');
select wiki_test.expect_eq('push: both drafts are still there',
  (select count(*)::int from wiki.draft), 2);

-- Pushing only the good path succeeds, leaving the conflicting draft behind.
create temporary table push_subset as
  select * from wiki.push('partial', array['root.engineering.guides.onboarding'::ltree]);

select wiki_test.expect_eq('push: a path subset publishes on its own',
  (select status::text from push_subset), 'published');
select wiki_test.expect_eq('push: and applies',
  (select content from wiki.current_document
    where path = 'root.engineering.guides.onboarding'), 'this one is fine');
select wiki_test.expect_eq('push: the unselected draft is untouched',
  (select count(*)::int from wiki.draft), 1);

delete from wiki.draft;
reset role;

------------------------------------------------------------------------------
-- Creating over a document you are not allowed to see.
--
-- The tempting hole: push() reports a create collision by handing back the
-- occupant's content, so guessing a path would be a way to read it. It does
-- not, and two independent things stop it. push() is SECURITY INVOKER, so the
-- existence probe and the content select are both RLS-filtered; and the
-- capability lattice puts read below create, so anyone entitled to create at a
-- path is entitled to read what is already there.
------------------------------------------------------------------------------
select wiki_test.login('erin');
set role fswiki_user;

insert into wiki.draft (author_id, operation, path, content)
values (wiki.current_user_id(), 'create', 'root.engineering.secret-plans', 'probe');
create temporary table push_probe as select * from wiki.push('probe');

select wiki_test.expect_eq('push: creating over an invisible document is refused',
  (select status::text from push_probe), 'forbidden');
select wiki_test.expect_eq('push: and discloses no content',
  (select server_content is null and base_content is null and server_version is null
     from push_probe), true);
delete from wiki.draft;
reset role;

-- Outside the role, because as erin the row is invisible and the count would be
-- zero whether or not push had eaten it.
select wiki_test.expect_eq('push: the occupant is untouched',
  (select count(*)::int from wiki.document where path = 'root.engineering.secret-plans'), 1);

-- The invariant the safety rests on, asserted directly rather than assumed:
-- nowhere may anyone create where they cannot read.
select wiki_test.expect_eq('nobody can create where they cannot read',
  (select count(*)::int from wiki.document d, wiki.principal p
    where p.kind = 'user'
      and wiki.has_capability(d.id, 'create', p.id)
      and not wiki.has_capability(d.id, 'read', p.id)),
  0);

------------------------------------------------------------------------------
-- A deny on the name itself, under a folder you may create in.
--
-- This used to abort the whole call with a bare RLS 42501 from the insert,
-- losing the report for every other document in the changeset. It has to come
-- back as one refused row.
------------------------------------------------------------------------------
insert into wiki.ace (document_id, principal_id, role_id, ace_type,
                      container_inherit, object_inherit)
select d.id, wiki_test.who('grace'), wiki.role_id('author'), 'allow', true, true
  from wiki.document d where d.path = 'root.engineering';
insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id, wiki_test.who('grace'), wiki.role_id('reader'), 'deny'
  from wiki.document d where d.path = 'root.engineering.secret-plans';

select wiki_test.login('grace');
set role fswiki_user;

insert into wiki.draft (author_id, operation, path, content)
values (wiki.current_user_id(), 'create', 'root.engineering.secret-plans', 'probe');
insert into wiki.draft (author_id, operation, path, content)
values (wiki.current_user_id(), 'create', 'root.engineering.grace-notes', 'fine');
create temporary table push_denied as select * from wiki.push('probe');

select wiki_test.expect_eq('push: a deny on the name is reported, not raised',
  (select status::text from push_denied where path = 'root.engineering.secret-plans'),
  'forbidden');
select wiki_test.expect_eq('push: the rest of the changeset is still reported',
  (select status::text from push_denied where path = 'root.engineering.grace-notes'),
  'published');
select wiki_test.expect_eq('push: but all-or-nothing still holds, so nothing landed',
  (select count(*)::int from wiki.document where path = 'root.engineering.grace-notes'),
  0);
delete from wiki.draft;
reset role;
delete from wiki.ace where principal_id = wiki_test.who('grace');

------------------------------------------------------------------------------
-- An unauthenticated caller cannot push at all.
------------------------------------------------------------------------------
select set_config('request.jwt.claims', '', false);
set role fswiki_user;
select wiki_test.expect_rejected('push: refuses an unauthenticated caller',
  $sql$select * from wiki.push()$sql$, '42501');
reset role;
