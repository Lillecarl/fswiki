-- Attachments: the same ACL, a real limit, and a flag that cannot lie.
--
-- Three things to prove, and the first is the reason the design is what it is.
--
--   1. **The ACL is not a second implementation.** An attachment is a
--      `wiki.document` row, so "who may fetch this file" is answered by the
--      policies that already exist. The way to check that is not to test the
--      attachment policies -- it is to show that the answer matches
--      `document_version`'s for the same document, over every user.
--
--   2. **The limit is the database's**, not the client's. A cap enforced in
--      the CLI is a cap, and a cap enforced in the CLI only is a suggestion:
--      psql is a client too.
--
--   3. **`is_attachment` follows the bytes.** It exists so `current_document`
--      need not join this table on the hot path, which makes it a
--      denormalisation -- and a denormalisation that can drift is a page that
--      renders as an empty file forever.
--
-- Nothing here runs inside a transaction. `wiki_test.result` is an ordinary
-- table and a ROLLBACK would take the verdicts with it; see 000_harness.sql.

------------------------------------------------------------------------------
-- A file in a folder alice owns, put there the way a client would
------------------------------------------------------------------------------

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect('alice may attach a file',
  (select created from wiki.attach('root.engineering.guides.diagram'::ltree,
                                   'image/png', '\x89504e470d0a1a0a'::bytea)));

select wiki_test.expect_eq('and the bytes came back the size they went in',
  (select byte_size from wiki.attachment_at('root.engineering.guides.diagram'::ltree)),
  8);

select wiki_test.expect_eq('with the media type she declared',
  (select media_type from wiki.attachment_at('root.engineering.guides.diagram'::ltree)),
  'image/png');

select wiki_test.expect_eq('and the bytes themselves, unchanged',
  (select bytes from wiki.attachment_at('root.engineering.guides.diagram'::ltree)),
  '\x89504e470d0a1a0a'::bytea);

-- The digest is generated, so it cannot disagree with the bytes.
select wiki_test.expect_eq('the digest is of the bytes',
  (select sha256 from wiki.attachment_at('root.engineering.guides.diagram'::ltree)),
  digest('\x89504e470d0a1a0a'::bytea, 'sha256'));

-- Replacing, rather than versioning. There is no history to check because
-- there is deliberately none to keep.
select wiki_test.expect('a second upload replaces rather than creates',
  not (select created from wiki.attach('root.engineering.guides.diagram'::ltree,
                                       'image/png', '\x0102030405'::bytea)));

select wiki_test.expect_eq('and the bytes are the new ones',
  (select byte_size from wiki.attachment_at('root.engineering.guides.diagram'::ltree)),
  5);

select wiki_test.expect_eq('there is still exactly one row for it',
  (select count(*)::int from wiki.attachment a
     join wiki.document d on d.id = a.document_id
    where d.path = 'root.engineering.guides.diagram'::ltree), 1);

reset role;

------------------------------------------------------------------------------
-- 1. The ACL is the document's, for everybody, in both directions
------------------------------------------------------------------------------
--
-- The assertion that matters. For every fixture user, the attachment is
-- visible exactly when the document it *is* would be readable -- which is what
-- `wiki.has_capability(document_id, 'read')` says, and is the same predicate
-- `document_version_select` uses. A disagreement in either direction is a bug:
-- one way the file is unreachable to somebody entitled to it, the other way it
-- has leaked.

do $$
declare
  who text;
  bad integer;
  total integer := 0;
begin
  foreach who in array array['alice','bob','carol','dave','erin','frank','grace'] loop
    perform wiki_test.login(who);
    set local role fswiki_user;
    select count(*)::int into bad from (
      select 1 where
        (select count(*) from wiki.attachment_at(
                              'root.engineering.guides.diagram'::ltree)) > 0
        is distinct from
        (select wiki.has_capability(
                  wiki_test.doc('root.engineering.guides.diagram'), 'read'))
    ) as difference;
    total := total + bad;
    if bad > 0 then
      raise notice 'the attachment disagrees with the document for %', who;
    end if;
    reset role;
  end loop;
  perform wiki_test.expect_eq(
    'an attachment is readable exactly when its document is', total, 0);
end $$;

reset role;

-- Named individually as well, because the cross product above would pass just
-- as happily if the answer were "nobody" for everyone.
select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect('bob reads the guides subtree, so he may fetch it',
  (select count(*) > 0 from wiki.attachment_at(
                            'root.engineering.guides.diagram'::ltree)));
reset role;

select wiki_test.login('erin');
set role fswiki_user;
select wiki_test.expect_eq('erin sees nothing in engineering, so no bytes either',
  (select count(*)::int from wiki.attachment_at(
                             'root.engineering.guides.diagram'::ltree)), 0);
-- And through the table rather than the function, because the function is a
-- convenience and the policy is the boundary.
select wiki_test.expect_eq('and not by reading the table directly either',
  (select count(*)::int from wiki.attachment), 0);
reset role;

-- Content does not follow a folder in. A folder visible only as a route must
-- not carry bytes any more than it carries a revision.
select wiki_test.login('erin');
set role fswiki_user;
select wiki_test.expect_eq('a traversal-only route yields no attachment',
  (select count(*)::int from wiki.attachment a
     join wiki.document d on d.id = a.document_id
    where d.path <@ 'root.engineering'::ltree), 0);
reset role;

------------------------------------------------------------------------------
-- 2. The limit belongs to the database
------------------------------------------------------------------------------

select wiki_test.expect_eq('there is a limit, and it is the seeded one',
  wiki.max_attachment_bytes(), 10485760::bigint);

-- Lowered to something a test can exceed without allocating ten megabytes.
update wiki.setting set value = '64' where key = 'max_attachment_bytes';

select wiki_test.expect_eq('an operator can change it', wiki.max_attachment_bytes(), 64::bigint);

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_rejected('a file over the limit is refused',
  $$select wiki.attach('root.engineering.guides.big'::ltree, 'application/pdf',
                       decode(repeat('00', 65), 'hex'))$$,
  '22001');

select wiki_test.expect('one at exactly the limit is not',
  (select created from wiki.attach('root.engineering.guides.exact'::ltree,
                                   'application/pdf', decode(repeat('00', 64), 'hex'))));

-- The trigger is on the table, not on the RPC. A client with a psql prompt is
-- still a client, and the whole point of enforcing it here is that there is no
-- way round it.
select wiki_test.expect_rejected('and the table refuses it too, not just the RPC',
  $$insert into wiki.attachment (document_id, bytes, media_type)
    values (wiki_test.doc('root.engineering.guides.exact'),
            decode(repeat('00', 65), 'hex'), 'application/pdf')$$,
  '22001');

-- Replacing an existing attachment goes through the same check.
select wiki_test.expect_rejected('growing one past the limit is refused as well',
  $$select wiki.attach('root.engineering.guides.exact'::ltree, 'application/pdf',
                       decode(repeat('00', 200), 'hex'))$$,
  '22001');

reset role;

-- Nobody the wiki serves may read the limit's row, let alone move it. A client
-- that could raise its own cap has no cap.
select wiki_test.expect_eq('no client role may read the settings table',
  (select count(*)::int
     from information_schema.role_table_grants g
    where g.table_schema = 'wiki' and g.table_name = 'setting'
      and g.grantee in ('fswiki_user', 'fswiki_anon', 'fswiki_authenticator',
                        'PUBLIC')), 0);

-- Stated as an allowlist rather than a denial, so it fails the day somebody
-- grants a new role instead of passing forever because the three named above
-- are still absent.
select wiki_test.expect_eq('the owner is the only grantee on it at all',
  (select coalesce(array_agg(distinct g.grantee::text order by g.grantee::text), '{}')
     from information_schema.role_table_grants g
    where g.table_schema = 'wiki' and g.table_name = 'setting'),
  array[(select tableowner::text from pg_tables
          where schemaname = 'wiki' and tablename = 'setting')]);

-- Put it back, so the tests after this see the wiki the rest of the suite does.
update wiki.setting set value = '10485760' where key = 'max_attachment_bytes';

------------------------------------------------------------------------------
-- 3. is_attachment follows the bytes
------------------------------------------------------------------------------

select wiki_test.expect_eq('every attachment row has the flag set',
  (select count(*)::int from wiki.attachment a
     join wiki.document d on d.id = a.document_id
    where not d.is_attachment), 0);

select wiki_test.expect_eq('and no other document has it',
  (select count(*)::int from wiki.document d
    where d.is_attachment
      and not exists (select 1 from wiki.attachment a where a.document_id = d.id)), 0);

-- The flag is set by a trigger on the table, so it follows a plain INSERT and
-- not only the RPC.
insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
select d.id, 'raw-upload', false, 'raw-upload', wiki_test.who('alice')
  from wiki.document d where d.path = 'root.engineering.guides'::ltree;

select wiki_test.expect('a document with no bytes is not an attachment',
  not (select is_attachment from wiki.document
        where path = 'root.engineering.guides.raw-upload'::ltree));

insert into wiki.attachment (document_id, bytes, media_type)
values (wiki_test.doc('root.engineering.guides.raw-upload'), '\x00'::bytea,
        'application/octet-stream');

select wiki_test.expect('and becomes one the moment the bytes arrive',
  (select is_attachment from wiki.document
    where path = 'root.engineering.guides.raw-upload'::ltree));

delete from wiki.attachment
 where document_id = wiki_test.doc('root.engineering.guides.raw-upload');

select wiki_test.expect('and stops being one when they go',
  not (select is_attachment from wiki.document
        where path = 'root.engineering.guides.raw-upload'::ltree));

delete from wiki.document where path = 'root.engineering.guides.raw-upload'::ltree;

-- A folder cannot be one, and the constraint says so rather than the code.
select wiki_test.expect_rejected('a folder cannot be an attachment',
  $$update wiki.document set is_attachment = true
     where path = 'root.engineering.guides'::ltree$$,
  '23514');

------------------------------------------------------------------------------
-- 4. One path space, and one kind of thing at each address
------------------------------------------------------------------------------

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_rejected('a file may not shadow a page',
  $$select wiki.attach('root.engineering.guides.onboarding'::ltree,
                       'image/png', '\x00'::bytea)$$,
  '23505');

select wiki_test.expect_rejected('and there is no folder to hold one at the root',
  $$select wiki.attach('root'::ltree, 'image/png', '\x00'::bytea)$$,
  '22023');

select wiki_test.expect_rejected('a folder that is not there is not named either',
  $$select wiki.attach('root.nowhere.at.all'::ltree, 'image/png', '\x00'::bytea)$$,
  '23503');

reset role;

-- And the reverse: a page may not shadow a file. The unique constraint on
-- document.path is what enforces it, which is the entire reason an attachment
-- is a document row rather than a table with a path of its own.
select wiki_test.expect_rejected('a page may not shadow a file',
  $$insert into wiki.document (parent_id, slug, is_folder, title)
    select d.id, 'diagram', false, 'Diagram' from wiki.document d
     where d.path = 'root.engineering.guides'::ltree$$,
  '23505');

------------------------------------------------------------------------------
-- 5. The media type cannot become a header
------------------------------------------------------------------------------
--
-- It is repeated in a Content-Type by the server. The constraint is what stops
-- a semicolon, a newline or a space getting that far, so the server never has
-- to sanitise a value the database could have refused.

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_rejected('a media type may not carry a parameter',
  $$select wiki.attach('root.engineering.guides.p1'::ltree,
                       'text/html; charset=utf-8', '\x00'::bytea)$$, '23514');

select wiki_test.expect_rejected('nor a newline',
  $$select wiki.attach('root.engineering.guides.p2'::ltree,
                       E'image/png\nX-Evil: yes', '\x00'::bytea)$$, '23514');

select wiki_test.expect_rejected('nor be empty',
  $$select wiki.attach('root.engineering.guides.p3'::ltree, '', '\x00'::bytea)$$,
  '23514');

select wiki_test.expect_rejected('nor omit the slash',
  $$select wiki.attach('root.engineering.guides.p4'::ltree, 'png', '\x00'::bytea)$$,
  '23514');

reset role;

------------------------------------------------------------------------------
-- 6. Removal is permanent, and says so
------------------------------------------------------------------------------
--
-- `purge`, through the document policy. An attachment has no revisions, so
-- there is no retire to offer instead, and a `delete` that turned out to be
-- irreversible would be the wrong surprise.

select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect('bob cannot purge what he did not create',
  not wiki.detach('root.engineering.guides.diagram'::ltree));
reset role;

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect('alice can', wiki.detach('root.engineering.guides.diagram'::ltree));
select wiki_test.expect_eq('and the document went with the bytes',
  (select count(*)::int from wiki.document
    where path = 'root.engineering.guides.diagram'::ltree), 0);
select wiki_test.expect('detaching what is not there is false, not an error',
  not wiki.detach('root.engineering.guides.never-existed'::ltree));
reset role;

-- Clean up the one left from the limit tests, so the closure and context files
-- see the tree they expect.
select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect('the probe file goes too',
  wiki.detach('root.engineering.guides.exact'::ltree));
reset role;

------------------------------------------------------------------------------
-- 7. The mirror does not see them
------------------------------------------------------------------------------
--
-- An attachment has no revision, so a client that mirrors `syncable_document`
-- would write a zero-byte file where a picture is. Until the FUSE driver can
-- carry bytes, the honest answer is to leave them out of the tree it copies.

select wiki_test.login('alice');
set role fswiki_user;
select wiki.attach('root.engineering.guides.chart'::ltree, 'image/png',
                   '\x89504e47'::bytea);

select wiki_test.expect('it is in the read tree',
  (select count(*) > 0 from wiki.current_document
    where path = 'root.engineering.guides.chart'::ltree));

select wiki_test.expect_eq('and not in the sync tree',
  (select count(*)::int from wiki.syncable_document
    where path = 'root.engineering.guides.chart'::ltree), 0);

select wiki_test.expect('the read tree says which kind it is',
  (select is_attachment from wiki.current_document
    where path = 'root.engineering.guides.chart'::ltree));

select wiki_test.expect('and a page says it is not one',
  not (select is_attachment from wiki.current_document
        where path = 'root.engineering.guides.onboarding'::ltree));

select wiki.detach('root.engineering.guides.chart'::ltree);
reset role;
