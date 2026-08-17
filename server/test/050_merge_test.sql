-- The merge lifecycle, and the audit trail.
--
-- Both are about the same thing from two directions: what the server will take
-- a client's word for, and what it will not.

------------------------------------------------------------------------------
-- begin_merge / resolve_merge / abort_merge
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'bob''s own text', 1
  from wiki.document d where d.path = 'root.engineering.guides.onboarding';

select wiki.begin_merge('root.engineering.guides.onboarding'::ltree,
                        'merged text with markers', 2, true);

select wiki_test.expect_eq('begin_merge marks the draft conflicted',
  (select state::text from wiki.draft), 'conflicted');
select wiki_test.expect_eq('begin_merge keeps the text it replaced',
  (select pre_merge_content from wiki.draft), 'bob''s own text');
select wiki_test.expect_eq('begin_merge records what was merged in',
  (select merged_from from wiki.draft), 2);
-- The rebase deliberately waits for the resolution. A conflicted draft that
-- already claimed the server's revision would become publishable the moment
-- someone deleted the markers without choosing a side.
select wiki_test.expect_eq('begin_merge leaves base_version alone',
  (select base_version from wiki.draft), 1);

-- Merging again must not lose the original. This is the difference between
-- "back out" meaning the pre-conflict text and meaning some intermediate mess.
select wiki.begin_merge('root.engineering.guides.onboarding'::ltree, 'second attempt', 2, true);
select wiki_test.expect_eq('merging twice still backs out to the original',
  (select pre_merge_content from wiki.draft), 'bob''s own text');

-- A conflicted draft is refused by push, whatever the client believes.
create temporary table push_unmerged as select * from wiki.push('nope');
select wiki_test.expect_eq('push refuses a conflicted draft',
  (select status::text from push_unmerged), 'unmerged');
select wiki_test.expect_eq('and the draft is still there',
  (select count(*)::int from wiki.draft), 1);

select wiki.abort_merge('root.engineering.guides.onboarding'::ltree);
select wiki_test.expect_eq('abort_merge restores the text exactly',
  (select content from wiki.draft), 'bob''s own text');
select wiki_test.expect_eq('abort_merge clears the state',
  (select state::text from wiki.draft), 'clean');
select wiki_test.expect_eq('abort_merge clears the backup',
  (select pre_merge_content is null and merged_from is null from wiki.draft), true);
select wiki_test.expect_eq('abort_merge does not touch base_version',
  (select base_version from wiki.draft), 1);

-- Resolving rebases onto what was merged in, and only then.
select wiki.begin_merge('root.engineering.guides.onboarding'::ltree, 'resolved text', 2, true);
select wiki.resolve_merge('root.engineering.guides.onboarding'::ltree);
select wiki_test.expect_eq('resolve_merge rebases onto the merged revision',
  (select base_version from wiki.draft), 2);
select wiki_test.expect_eq('resolve_merge keeps the resolved text',
  (select content from wiki.draft), 'resolved text');
select wiki_test.expect_eq('resolve_merge clears the state',
  (select state::text from wiki.draft), 'clean');

delete from wiki.draft;
reset role;

-- One author cannot touch another's merge. The functions run as the invoker, so
-- RLS filters the UPDATE to nothing rather than raising.
select wiki_test.login('bob');
set role fswiki_user;
insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
select wiki.current_user_id(), 'update', d.id, d.path, 'bob''s', 1
  from wiki.document d where d.path = 'root.engineering.guides.onboarding';
reset role;

select wiki_test.login('frank');
set role fswiki_user;
select wiki_test.expect_eq('a merge cannot be started on someone else''s draft',
  (select count(*)::int from wiki.begin_merge(
     'root.engineering.guides.onboarding'::ltree, 'frank was here', 2, true)),
  0);
reset role;

select wiki_test.expect_eq('and the draft is untouched',
  (select content from wiki.draft), 'bob''s');
delete from wiki.draft;

------------------------------------------------------------------------------
-- Access events
--
-- The rule that matters: the server takes the *principal* from the token and
-- everything about the process from the payload, because only one of those two
-- is something the client could not have made up.
------------------------------------------------------------------------------
select wiki_test.login('bob');
set role fswiki_user;

select wiki_test.expect_eq('record_opens accepts a batch',
  wiki.record_opens(jsonb_build_array(
    jsonb_build_object('event_id', '11111111-1111-1111-1111-111111111111',
                       'document_id', wiki_test.doc('root.engineering.guides.onboarding'),
                       'path', 'root.engineering.guides.onboarding',
                       'occurred_at', '2026-08-18T00:00:00Z',
                       'action', 'open', 'open_flags', 32768,
                       'process', jsonb_build_object('comm', 'vim', 'pid', 1234)),
    jsonb_build_object('event_id', '22222222-2222-2222-2222-222222222222',
                       'path', 'root.public.welcome',
                       'occurred_at', '2026-08-18T00:00:01Z', 'action', 'open'))),
  2);

select wiki_test.expect_eq('resending the same batch adds nothing',
  wiki.record_opens(jsonb_build_array(
    jsonb_build_object('event_id', '11111111-1111-1111-1111-111111111111',
                       'path', 'root.engineering.guides.onboarding',
                       'occurred_at', '2026-08-18T00:00:00Z', 'action', 'open'))),
  0);

select wiki_test.expect_eq('the event is filed against the token holder',
  (select p.name from wiki.access_event e join wiki.principal p on p.id = e.principal_id
    where e.event_id = '11111111-1111-1111-1111-111111111111'),
  'bob');
select wiki_test.expect_eq('the process claim is kept as given',
  (select process ->> 'comm' from wiki.access_event
    where event_id = '11111111-1111-1111-1111-111111111111'),
  'vim');
select wiki_test.expect_eq('bob sees his own trail',
  (select count(*)::int from wiki.access_event), 2);

-- Filing against someone else must fail the WITH CHECK, not quietly succeed.
select wiki_test.expect_rejected('an event cannot be filed against another principal',
  format($sql$insert into wiki.access_event
                (event_id, principal_id, occurred_at, action)
              values ('33333333-3333-3333-3333-333333333333', %L, now(), 'open')$sql$,
         wiki_test.who('frank')));

-- A trail its subject can rewrite is not one. This is withheld at the grant
-- rather than by a policy, so it raises instead of quietly filtering to zero
-- rows — the louder of the two failures, and the right one here.
select wiki_test.expect_rejected('the trail cannot be edited by its subject',
  $sql$update wiki.access_event set action = 'open'$sql$);
select wiki_test.expect_rejected('nor deleted by its subject',
  $sql$delete from wiki.access_event$sql$);
select wiki_test.expect_eq('so both events are still there',
  (select count(*)::int from wiki.access_event), 2);

reset role;

select wiki_test.login('frank');
set role fswiki_user;
select wiki_test.expect_eq('nobody reads anybody else''s trail',
  (select count(*)::int from wiki.access_event), 0);
reset role;


