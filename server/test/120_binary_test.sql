-- A revision that holds bytes.
--
-- 140_attachments.sql put attachments in a table of their own; 150 folded them
-- into `document_version`, which is what this file tests. The claim being made
-- is a strong one and it is what makes the mount possible: **a file is a
-- revision**, so every property a page already has, a picture has too.
--
-- So most of what follows is not "attachments work". It is "a picture behaves
-- exactly like a page" -- history, point-in-time reads, tombstones, drafts,
-- push, the ACL, the sync tree -- because a second, worse copy of each is what
-- folding it in was meant to avoid.
--
-- Nothing here runs inside a transaction. `wiki_test.result` is an ordinary
-- table and a ROLLBACK would take the verdicts with it; see 000_harness.sql.

------------------------------------------------------------------------------
-- 1. Putting one there
------------------------------------------------------------------------------

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_eq('attaching a file publishes revision 1',
  (select version from wiki.attach('root.engineering.guides.diagram'::ltree,
                                   'image/png', '\x89504e470d0a1a0a'::bytea)), 1);

select wiki_test.expect_eq('the media type is the content type',
  (select content_type from wiki.current_document
    where path = 'root.engineering.guides.diagram'::ltree), 'image/png');

select wiki_test.expect_eq('the bytes came back unchanged',
  (select content_bytes from wiki.current_document
    where path = 'root.engineering.guides.diagram'::ltree),
  '\x89504e470d0a1a0a'::bytea);

select wiki_test.expect('there is no text body pretending to be one',
  (select content is null from wiki.current_document
    where path = 'root.engineering.guides.diagram'::ltree));

select wiki_test.expect('and the view says which kind it is',
  (select is_binary from wiki.current_document
    where path = 'root.engineering.guides.diagram'::ltree));

select wiki_test.expect('while a page says it is not',
  (select not is_binary from wiki.current_document
    where path = 'root.engineering.guides.onboarding'::ltree));

-- Size and hash are the trigger's, not the caller's. A client can stat a file
-- from the manifest without fetching it, which is what a mount does on every
-- listing.
select wiki_test.expect_eq('the size is measured, not claimed',
  (select size from wiki.current_document
    where path = 'root.engineering.guides.diagram'::ltree), 8::bigint);

select wiki_test.expect_eq('and the hash is of the bytes',
  (select content_hash from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.diagram')
      and upper_inf(valid)),
  digest('\x89504e470d0a1a0a'::bytea, 'sha256'));

reset role;

-- The claim tables/150 makes when it drops the generated column: a page's hash
-- does not change. pgcrypto hashes text in the database encoding, so on UTF8
-- that is byte-for-byte what convert_to() produces -- and this asserts it
-- rather than trusting the paragraph that says so.
select wiki_test.expect('hashing text and hashing its UTF8 bytes agree',
  digest('a page with an umlaut: ö', 'sha256')
  = digest(convert_to('a page with an umlaut: ö', 'UTF8'), 'sha256'));

select wiki_test.expect_eq('every existing page kept its hash',
  (select count(*)::int from wiki.document_version
    where content is not null
      and content_hash is distinct from digest(content, 'sha256')), 0);

------------------------------------------------------------------------------
-- 2. A file has history, which is the whole point of folding it in
------------------------------------------------------------------------------

select wiki_test.login('alice');
set role fswiki_user;
select wiki.attach('root.engineering.guides.diagram'::ltree, 'image/png',
                   '\x0102030405'::bytea, 'redrawn');
reset role;

select wiki_test.expect_eq('replacing it is a second revision, not an overwrite',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.diagram')), 2);

select wiki_test.expect_eq('the live one is the new bytes',
  (select content_bytes from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.diagram')
      and upper_inf(valid)), '\x0102030405'::bytea);

select wiki_test.expect_eq('and the old bytes are still there',
  (select content_bytes from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.diagram')
      and version = 1), '\x89504e470d0a1a0a'::bytea);

-- The temporal model, which a separate table could not have joined. The wiki
-- as of any instant is one predicate across every document at once, and that
-- has to include the pictures or it is not the wiki as of that instant.
select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect_eq('a point-in-time read shows the picture as it was',
  (select content_bytes from wiki.document_as_of(
     (select lower(valid) + interval '1 microsecond'
        from wiki.document_version
       where document_id = wiki_test.doc('root.engineering.guides.diagram')
         and version = 1))
    where id = wiki_test.doc('root.engineering.guides.diagram')),
  '\x89504e470d0a1a0a'::bytea);
reset role;

------------------------------------------------------------------------------
-- 3. Removing one is a retirement, not a deletion
------------------------------------------------------------------------------
--
-- Under 140 an attachment had no history, so removal had to be permanent and
-- asked for `purge`. It has history now, so this is an ordinary tombstone and
-- asks for `delete` -- and putting it back is another revision rather than an
-- apology.

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect('detaching it reports that it did something',
  wiki.detach('root.engineering.guides.diagram'::ltree));
reset role;

select wiki_test.expect_eq('which was a tombstone revision',
  (select is_tombstone from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.diagram')
      and upper_inf(valid)), true);

select wiki_test.expect_eq('the document row survives',
  (select count(*)::int from wiki.document
    where path = 'root.engineering.guides.diagram'::ltree), 1);

select wiki_test.expect_eq('and so do the bytes, in history',
  (select count(*)::int from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.diagram')
      and content_bytes is not null), 2);

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect_eq('but it is gone from the live tree',
  (select count(*)::int from wiki.current_document
    where path = 'root.engineering.guides.diagram'::ltree), 0);

select wiki_test.expect('and putting it back is just another revision',
  (select version from wiki.attach('root.engineering.guides.diagram'::ltree,
                                   'image/png', '\x99'::bytea)) = 4);
select wiki_test.expect('detaching nothing is false, not an error',
  not wiki.detach('root.engineering.guides.never-existed'::ltree));
reset role;

------------------------------------------------------------------------------
-- 4. The ACL is the document's, for everybody, in both directions
------------------------------------------------------------------------------
--
-- There are no attachment policies any more, which is what this checks. A
-- binary revision is admitted by `document_version_select` -- the same policy,
-- not a mirror of it -- so a picture is readable exactly when the page beside
-- it would be.

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
        (select count(*) from wiki.current_document
          where path = 'root.engineering.guides.diagram'::ltree) > 0
        is distinct from
        (select wiki.has_capability(
                  wiki_test.doc('root.engineering.guides.diagram'), 'read'))
    ) as difference;
    total := total + bad;
    if bad > 0 then
      raise notice 'the file disagrees with its document for %', who;
    end if;
    reset role;
  end loop;
  perform wiki_test.expect_eq(
    'a file is readable exactly when its document is', total, 0);
end $$;

reset role;

select wiki_test.login('erin');
set role fswiki_user;
select wiki_test.expect_eq('erin sees nothing in engineering, so no bytes either',
  (select count(*)::int from wiki.document_version
    where content_bytes is not null), 0);
reset role;

------------------------------------------------------------------------------
-- 5. A file is in the tree a mirror copies
------------------------------------------------------------------------------
--
-- 140 had to leave attachments out of syncable_document, because they had no
-- revision and a mirror would have written a zero-byte file where a picture
-- was. They are revisions now, so there is nothing to leave out.

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect('a file is in the sync tree',
  (select count(*) > 0 from wiki.syncable_document
    where path = 'root.engineering.guides.diagram'::ltree));

select wiki_test.expect_eq('with a size a mount can stat it by',
  (select size from wiki.syncable_document
    where path = 'root.engineering.guides.diagram'::ltree), 1::bigint);

select wiki_test.expect_eq('and the audited read hands back the bytes',
  (select content_bytes from wiki.read_document(
     wiki_test.doc('root.engineering.guides.diagram'), null)), '\x99'::bytea);

select wiki_test.expect('while the text column stays null',
  (select content is null from wiki.read_document(
     wiki_test.doc('root.engineering.guides.diagram'), null)));

reset role;

------------------------------------------------------------------------------
-- 6. One body, and never two
------------------------------------------------------------------------------

-- Each probe below names a document that already has a live revision, so it
-- carries a closed historical `valid`. Without one the exclusion constraint
-- fires first and the test passes for the wrong reason.
select wiki_test.expect_rejected('a revision may not be text and bytes at once',
  $$insert into wiki.document_version (document_id, version, path, content,
                                       content_bytes, content_type, valid)
    values (wiki_test.doc('root.engineering.guides.onboarding'), 99,
            'root.engineering.guides.onboarding'::ltree, 'text', '\x00'::bytea,
            'image/png', tstzrange('2000-01-01', '2000-01-02'))$$,
  '23514');

select wiki_test.expect_rejected('a tombstone may not carry bytes',
  $$insert into wiki.document_version (document_id, version, path,
                                       content_bytes, is_tombstone, valid)
    values (wiki_test.doc('root.engineering.guides.onboarding'), 98,
            'root.engineering.guides.onboarding'::ltree, '\x00'::bytea, true,
            tstzrange('2000-01-01', '2000-01-02'))$$,
  '23514');

select wiki_test.expect_rejected('a draft may not be text and bytes at once',
  $$insert into wiki.draft (author_id, operation, path, content, content_bytes)
    values (wiki_test.who('alice'), 'create', 'root.both'::ltree, 'x',
            '\x00'::bytea)$$,
  '23514');

-- A draft of bytes is a real draft, which is what makes the mount's promise
-- true for a picture: everything you do there is a draft until you push.
insert into wiki.draft (author_id, operation, path, content_bytes, content_type)
values (wiki_test.who('alice'), 'create', 'root.public.fresh'::ltree,
        '\xdeadbeef'::bytea, 'image/png');

select wiki_test.expect_eq('a draft may be bytes with no text at all',
  (select octet_length(content_bytes) from wiki.draft
    where path = 'root.public.fresh'::ltree), 4);

delete from wiki.draft where path = 'root.public.fresh'::ltree;

------------------------------------------------------------------------------
-- 7. Somewhere else entirely
------------------------------------------------------------------------------
--
-- No backend but 'database' is implemented, and the shape is still enforced --
-- which is the point of testing it now rather than when one arrives. A row
-- that says its bytes are elsewhere must carry a locator and must not carry
-- bytes, and it must bring its own size and hash, because nothing in this
-- database ever saw them.

insert into wiki.storage_backend (name, description)
values ('test_bucket', 'A backend that exists only for this file.');

select wiki_test.expect_rejected('an unknown backend is refused',
  $$insert into wiki.document_version (document_id, version, path, storage,
                                       locator, content_type, byte_size,
                                       content_hash, valid)
    values (wiki_test.doc('root.engineering.guides.onboarding'), 97,
            'root.engineering.guides.onboarding'::ltree, 'no_such_place',
            's3://x/y', 'image/png', 10, '\x00'::bytea,
            tstzrange('2000-01-01', '2000-01-02'))$$,
  '23503');

select wiki_test.expect_rejected('external storage with no locator is refused',
  $$insert into wiki.document_version (document_id, version, path, storage,
                                       content_type, byte_size, content_hash,
                                       valid)
    values (wiki_test.doc('root.engineering.guides.onboarding'), 96,
            'root.engineering.guides.onboarding'::ltree, 'test_bucket',
            'image/png', 10, '\x00'::bytea,
            tstzrange('2000-01-01', '2000-01-02'))$$,
  '23514');

select wiki_test.expect_rejected('external storage that also keeps bytes is refused',
  $$insert into wiki.document_version (document_id, version, path, storage,
                                       locator, content_bytes, content_type,
                                       byte_size, content_hash, valid)
    values (wiki_test.doc('root.engineering.guides.onboarding'), 95,
            'root.engineering.guides.onboarding'::ltree, 'test_bucket',
            's3://x/y', '\x00'::bytea, 'image/png', 10, '\x00'::bytea,
            tstzrange('2000-01-01', '2000-01-02'))$$,
  '23514');

select wiki_test.expect_rejected('and one that brings no size or hash is refused',
  $$insert into wiki.document_version (document_id, version, path, storage,
                                       locator, content_type, valid)
    values (wiki_test.doc('root.engineering.guides.onboarding'), 94,
            'root.engineering.guides.onboarding'::ltree, 'test_bucket',
            's3://x/y', 'image/png', tstzrange('2000-01-01', '2000-01-02'))$$,
  '22023');

-- The shape that works, and what a client is told about it.
insert into wiki.document_version (document_id, version, path, storage, locator,
                                   content_type, byte_size, content_hash, valid)
values (wiki_test.doc('root.io-test.child'), 50, 'root.io-test.child'::ltree,
        'test_bucket', 's3://bucket/key', 'image/png', 4096,
        digest('pretend', 'sha256'), tstzrange('2000-01-01', '2000-01-02'));

select wiki_test.expect_eq('an external revision keeps the size it was given',
  (select byte_size from wiki.document_version
    where document_id = wiki_test.doc('root.io-test.child') and version = 50),
  4096::bigint);

select wiki_test.expect('and it counts as binary, with nowhere local to look',
  (select storage <> 'database' and content is null and content_bytes is null
     from wiki.document_version
    where document_id = wiki_test.doc('root.io-test.child') and version = 50));

delete from wiki.document_version
 where document_id = wiki_test.doc('root.io-test.child') and version = 50;
delete from wiki.storage_backend where name = 'test_bucket';

------------------------------------------------------------------------------
-- 8. The limit belongs to the database
------------------------------------------------------------------------------

select wiki_test.expect_eq('there is a limit, and it is the seeded one',
  wiki.max_attachment_bytes(), 10485760::bigint);

update wiki.setting set value = '64' where key = 'max_attachment_bytes';

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_rejected('a file over the limit is refused',
  $$select wiki.attach('root.engineering.guides.big'::ltree, 'application/pdf',
                       decode(repeat('00', 65), 'hex'))$$,
  '22001');

select wiki_test.expect_eq('one at exactly the limit is not',
  (select byte_size from wiki.attach('root.engineering.guides.exact'::ltree,
                                     'application/pdf',
                                     decode(repeat('00', 64), 'hex'))),
  64::bigint);

-- On the table, not in the RPC. A client with a psql prompt is still a client.
select wiki_test.expect_rejected('the table refuses it too, not just wiki.attach',
  $$insert into wiki.document_version (document_id, version, path,
                                       content_bytes, content_type, valid)
    values (wiki_test.doc('root.engineering.guides.exact'), 40,
            'root.engineering.guides.exact'::ltree,
            decode(repeat('00', 65), 'hex'), 'application/pdf',
            tstzrange('2000-01-01', '2000-01-02'))$$,
  '22001');

-- And on the draft, so a file too big to publish is refused when it is
-- written rather than at push time, with the person long gone.
select wiki_test.expect_rejected('a draft over the limit is refused when written',
  $$insert into wiki.draft (author_id, operation, path, content_bytes,
                            content_type)
    values (wiki_test.who('alice'), 'create', 'root.public.huge'::ltree,
            decode(repeat('00', 65), 'hex'), 'image/png')$$,
  '22001');

-- Text is not capped by this. A page is bounded by what a person will type and
-- a wiki that refused a long article because of a picture limit would be
-- absurd.
select wiki_test.expect_eq('a long page is not a big file',
  (select version from wiki.attach('root.engineering.guides.essay'::ltree,
                                   'text/markdown', null)), 1);

reset role;

update wiki.setting set value = '10485760' where key = 'max_attachment_bytes';

select wiki_test.expect_eq('no client role may read the settings table',
  (select count(*)::int
     from information_schema.role_table_grants g
    where g.table_schema = 'wiki' and g.table_name = 'setting'
      and g.grantee in ('fswiki_user', 'fswiki_anon', 'fswiki_authenticator',
                        'PUBLIC')), 0);

select wiki_test.expect('but the list of backends is not a secret',
  has_table_privilege('fswiki_user', 'wiki.storage_backend', 'select')
  and has_table_privilege('fswiki_anon', 'wiki.storage_backend', 'select'));

select wiki_test.expect_eq('and no client role may add one',
  (select count(*)::int
     from information_schema.role_table_grants g
    where g.table_schema = 'wiki' and g.table_name = 'storage_backend'
      and g.privilege_type in ('INSERT', 'UPDATE', 'DELETE')
      and g.grantee in ('fswiki_user', 'fswiki_anon', 'fswiki_authenticator',
                        'PUBLIC')), 0);

------------------------------------------------------------------------------
-- 9. What 140 left behind is gone
------------------------------------------------------------------------------

select wiki_test.expect_eq('there is no attachment table any more',
  (select count(*)::int from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'wiki' and c.relname = 'attachment'), 0);

select wiki_test.expect_eq('nor a denormalised flag to drift',
  (select count(*)::int from information_schema.columns
    where table_schema = 'wiki' and table_name = 'document'
      and column_name = 'is_attachment'), 0);

-- Clean up, so the files after this see the tree they expect.
select wiki_test.login('alice');
set role fswiki_user;
select wiki.detach('root.engineering.guides.diagram'::ltree);
select wiki.detach('root.engineering.guides.exact'::ltree);
select wiki.detach('root.engineering.guides.essay'::ltree);
reset role;
