-- wiki.search: a hand-applied policy, checked against the one it copies.
--
-- `wiki.search()` is SECURITY DEFINER. It reads `document_version` as the
-- owner, with RLS switched off, and then applies `wiki.can_ctx` itself. There
-- is a measured reason for that -- runtime/078_search.sql has the numbers --
-- and there is no getting around what it costs: **the ACL is now written
-- twice**, and a copy that drifts from its original is a page leaking to
-- someone the policy would have refused.
--
-- So the first section is not a spot check. For every fixture user and every
-- word in the fixtures, what the function returns must equal what the RLS view
-- returns, in both directions of disagreement. A missing row is a bug; an
-- extra row is a disclosure.
--
-- The rest tests the properties a search engine adds that an ACL does not:
-- that a hidden page changes nothing an outsider can observe, that a retired
-- page stops being findable, that the excerpt carries no markup, and that
-- drafts are their author's alone.

------------------------------------------------------------------------------
-- The reference: what a reader can see, matched the way the index matches
------------------------------------------------------------------------------
--
-- Deliberately built from `wiki.current_document`, which is security_invoker
-- and therefore filtered by the policies rather than by this file's idea of
-- them. The expression is the one `document_version.search` is generated from,
-- so the two agree on *what matches* and can only disagree on *who may see it*
-- -- which is the single question these assertions exist to ask.
create or replace function wiki_test.visible_matches(p_query text)
returns setof ltree
language sql stable
set search_path = wiki_test, wiki, public, pg_temp as $$
  select d.path
    from wiki.current_document d
   where d.version is not null
     and to_tsvector('english',
           replace(replace(d.path::text, '.', ' '), '-', ' ') || ' ' ||
           left(coalesce(d.content, ''), 262144))
         @@ websearch_to_tsquery('english', p_query)
   order by d.path;
$$;

grant execute on function wiki_test.visible_matches(text) to fswiki_user, fswiki_anon;

-- One assertion, run for one user and one query. Returns the number of
-- documents the two disagree about, which is zero or a bug.
create or replace function wiki_test.search_disagreement(p_query text)
returns integer
language sql stable
set search_path = wiki_test, wiki, public, pg_temp as $$
  select count(*)::int from (
    (select path from wiki.search(p_query, 100)
      except select wiki_test.visible_matches(p_query))
    union all
    (select wiki_test.visible_matches(p_query)
      except select path from wiki.search(p_query, 100))
  ) as difference;
$$;

grant execute on function wiki_test.search_disagreement(text) to fswiki_user, fswiki_anon;

------------------------------------------------------------------------------
-- 1. The copy is the original, for everyone, over every word
------------------------------------------------------------------------------
--
-- Seven users against the whole fixture vocabulary. `contents` is in five
-- pages with five different ACLs; `locked` is in exactly one, which only alice
-- may read; `welcome` is public. Between them every branch of the ACL that
-- matters to a reader is exercised, and the cross product below runs each user
-- against all of them at once rather than naming pairs.

do $$
declare
  who text;
  word text;
  bad integer;
  total integer := 0;
begin
  foreach who in array array['alice','bob','carol','dave','erin','frank','grace'] loop
    perform wiki_test.login(who);
    set local role fswiki_user;
    foreach word in array array['contents', 'locked', 'welcome', 'memo',
                                'secret plans', 'onboarding', 'test',
                                'engineering', 'fine', 'revision',
                                'notices', 'bulletin', 'private'] loop
      select wiki_test.search_disagreement(word) into bad;
      total := total + bad;
      if bad > 0 then
        raise notice 'search disagrees for % on %: % documents', who, word, bad;
      end if;
    end loop;
    reset role;
  end loop;
  perform wiki_test.expect_eq(
    'search returns exactly what the RLS view returns, for every user and word',
    total, 0);
end $$;

reset role;

------------------------------------------------------------------------------
-- 2. A page you may not read is not merely absent -- it is invisible
------------------------------------------------------------------------------
--
-- `root.locked` holds the only occurrence of the word `locked`. Alice reads
-- it; nobody else does. So for everyone else the word must be indistinguishable
-- from a word that is in no page at all -- same rows, same count, no hint that
-- there was something to refuse.

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect_eq('alice: finds the page only she may read',
  (select array_agg(path::text order by path) from wiki.search('locked', 100)),
  array['root.locked']);
reset role;

select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect_eq('bob: the same word finds nothing',
  (select count(*)::int from wiki.search('locked', 100)), 0);
-- The comparison, rather than two separate zeroes: "no results for a word in a
-- page you may not read" and "no results for a word in no page" have to be the
-- same answer, and a test that asserts each is zero would still pass if one of
-- them started returning a count.
select wiki_test.expect_eq('bob: and is the same answer as a word nobody wrote',
  (select array_agg(path::text order by path) from wiki.search('locked', 100)),
  (select array_agg(path::text order by path)
     from wiki.search('unguessableword', 100)));
reset role;

-- Erin sees two pages. The engineering tree is not hers, and nothing in it may
-- reach her through search -- including the fact that it has anything in it.
select wiki_test.login('erin');
set role fswiki_user;
select wiki_test.expect_eq('erin: search reaches only her two pages',
  (select array_agg(path::text order by path) from wiki.search('contents', 100)),
  array['root.bulletin', 'root.notices']);
select wiki_test.expect_eq('erin: and the engineering tree does not exist to her',
  (select count(*)::int from wiki.search('engineering secret plans', 100)), 0);
reset role;

------------------------------------------------------------------------------
-- 3. Content does not follow a folder in
------------------------------------------------------------------------------
--
-- `document_version_select` re-tests `read` rather than leaning on the
-- document policy, because that policy also admits folders you may only
-- traverse. The hand-applied copy has to keep that distinction: a folder
-- visible only as a route must contribute nothing to a result list.

select wiki_test.login('erin');
set role fswiki_user;
select wiki_test.expect_eq('a folder never appears as a search result',
  (select count(*)::int
     from wiki.search('root', 100) s
     join wiki.document d on d.id = s.document_id
    where d.is_folder), 0);
reset role;

-- And the same, stated over every user at once: search only ever returns rows
-- that `wiki.current_document` would have returned with content attached.
do $$
declare who text; bad integer; total integer := 0;
begin
  foreach who in array array['alice','bob','carol','dave','erin','frank','grace'] loop
    perform wiki_test.login(who);
    set local role fswiki_user;
    select count(*)::int into bad
      from wiki.search('contents locked welcome memo onboarding', 100) s
     where not exists (select 1 from wiki.current_document d
                        where d.id = s.document_id and d.version is not null);
    total := total + bad;
    reset role;
  end loop;
  perform wiki_test.expect_eq('every result is a readable published document',
    total, 0);
end $$;

reset role;

------------------------------------------------------------------------------
-- 4. A query that says nothing gets nothing
------------------------------------------------------------------------------
--
-- Empty, blank, null and all-stopwords are four ways to write a tsquery with
-- no lexemes in it. None of them may become "everything", which is what a
-- naive `@@` against an empty query would do in some engines.

select wiki_test.login('alice');
set role fswiki_user;

select wiki_test.expect_eq('an empty query matches nothing',
  (select count(*)::int from wiki.search('', 100)), 0);
select wiki_test.expect_eq('a blank query matches nothing',
  (select count(*)::int from wiki.search('     ', 100)), 0);
select wiki_test.expect_eq('a null query matches nothing',
  (select count(*)::int from wiki.search(null, 100)), 0);
select wiki_test.expect_eq('a query of nothing but stopwords matches nothing',
  (select count(*)::int from wiki.search('the of and', 100)), 0);

-- The limit is the caller's to choose and the schema's to cap, because every
-- row costs an ACL test.
select wiki_test.expect_eq('a limit of zero still returns a row',
  (select count(*)::int from wiki.search('contents', 0)), 1);
select wiki_test.expect_eq('a negative limit does not return the wiki',
  (select count(*)::int from wiki.search('contents', -5)), 1);
select wiki_test.expect('an absurd limit is capped rather than obeyed',
  (select count(*) from wiki.search('contents', 100000)) <= 100);
select wiki_test.expect_eq('a null limit falls back to the default',
  (select count(*)::int from wiki.search('contents', null)) > 0, true);

------------------------------------------------------------------------------
-- 5. The excerpt is text, and it is delimited by things that are not markup
------------------------------------------------------------------------------
--
-- ts_headline does not escape what it is given, so its output is document text
-- with markers in it. If the markers were tags, a page written by one person
-- would arrive at another person's browser as markup. They are STX and ETX,
-- which no HTML parser will ever act on, and the client escapes the text
-- before turning them into a tag.

select wiki_test.expect('an excerpt is delimited with control characters',
  (select excerpt like '%' || chr(2) || '%' and excerpt like '%' || chr(3) || '%'
     from wiki.search('contents', 1)));

select wiki_test.expect_eq('and never with a tag of its own',
  (select count(*)::int from wiki.search('contents locked welcome memo', 100)
    where excerpt like '%<%'), 0);

reset role;

------------------------------------------------------------------------------
-- 6. A retired page stops being findable
------------------------------------------------------------------------------
--
-- Retirement is a tombstone version, not a deletion, so the old revision and
-- its search vector are both still on disk.
--
-- On a probe document of its own, cleaned up by hand afterwards. Not inside a
-- transaction: `wiki_test.result` is an ordinary table and a ROLLBACK would
-- take these two verdicts with it. See 000_harness.sql.

insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
select d.id, 'search-probe', false, 'Search Probe', wiki_test.who('alice')
  from wiki.document d where d.path = 'root'::ltree;

insert into wiki.document_version (document_id, version, path, content,
                                   message, author_id)
values (wiki_test.doc('root.search-probe'), 1, 'root.search-probe'::ltree,
        'a page about aardvarks', 'initial', wiki_test.who('alice'));

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect_eq('a published page is findable',
  (select array_agg(path::text) from wiki.search('aardvarks', 100)),
  array['root.search-probe']);
reset role;

update wiki.document_version
   set valid = tstzrange(lower(valid), now())
 where document_id = wiki_test.doc('root.search-probe') and upper_inf(valid);
insert into wiki.document_version (document_id, version, path, content,
                                   is_tombstone, message, author_id)
values (wiki_test.doc('root.search-probe'), 2, 'root.search-probe'::ltree, null,
        true, 'retired', wiki_test.who('alice'));

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect_eq('and stops being findable once retired',
  (select count(*)::int from wiki.search('aardvarks', 100)), 0);
reset role;

-- The versions go with it, by cascade.
delete from wiki.document where path = 'root.search-probe'::ltree;

------------------------------------------------------------------------------
-- 7. Drafts belong to their author and to nobody else
------------------------------------------------------------------------------

insert into wiki.draft (author_id, operation, path, content, message)
values (wiki_test.who('bob'), 'create', 'root.bobs-draft'::ltree,
        'a draft about zeppelins', 'wip'),
       (wiki_test.who('alice'), 'create', 'root.alices-draft'::ltree,
        'another draft about zeppelins', 'wip');

select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect_eq('bob finds his own draft',
  (select array_agg(path::text order by path)
     from wiki.search_drafts('zeppelins', 100)),
  array['root.bobs-draft']);
select wiki_test.expect_eq('and the published search does not hold it',
  (select count(*)::int from wiki.search('zeppelins', 100)), 0);
reset role;

select wiki_test.login('alice');
set role fswiki_user;
select wiki_test.expect_eq('alice finds hers and not his',
  (select array_agg(path::text order by path)
     from wiki.search_drafts('zeppelins', 100)),
  array['root.alices-draft']);
reset role;

delete from wiki.draft
 where path in ('root.bobs-draft'::ltree, 'root.alices-draft'::ltree);

------------------------------------------------------------------------------
-- 8. The grants
------------------------------------------------------------------------------
--
-- `search` takes no principal argument, so there is no way to phrase "what
-- would somebody else find". `search_drafts` is not anonymous business at all:
-- an anonymous caller has no account, so it would return nothing -- and a
-- grant that returns nothing is still a wider surface than no grant.

select wiki_test.expect('a signed-in caller may search',
  has_function_privilege('fswiki_user', 'wiki.search(text, integer)', 'execute'));
select wiki_test.expect('an anonymous caller may search',
  has_function_privilege('fswiki_anon', 'wiki.search(text, integer)', 'execute'));
select wiki_test.expect_eq('an anonymous caller may not search drafts',
  has_function_privilege('fswiki_anon', 'wiki.search_drafts(text, integer)',
                         'execute'), false);

select wiki_test.expect_eq('search takes no principal argument',
  (select coalesce(array_agg(p.proname::text), '{}')
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'wiki' and p.proname like 'search%'
      and 'p_user' = any(coalesce(p.proargnames, '{}'::text[]))),
  '{}'::text[]);

-- The one that carries the risk is the one that had a reason. `search_drafts`
-- reads a table whose policy is already an equality test, so it stays an
-- ordinary invoker function -- and this assertion is what stops the pattern
-- spreading by imitation.
select wiki_test.expect_eq('only wiki.search is SECURITY DEFINER',
  (select array_agg(p.proname::text order by p.proname)
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'wiki' and p.proname like 'search%' and p.prosecdef),
  array['search']);

------------------------------------------------------------------------------
-- 9. The index is doing the work
------------------------------------------------------------------------------
--
-- A generated column cannot go stale, but it can be generated from the wrong
-- thing. These say it holds both halves of what runtime/078 claims.

select wiki_test.expect('the search vector holds the content',
  (select search @@ websearch_to_tsquery('english', 'welcome')
     from wiki.document_version
    where document_id = wiki_test.doc('root.public.welcome') and upper_inf(valid)));

select wiki_test.expect('and the path, so a page is findable by its own name',
  (select search @@ websearch_to_tsquery('english', 'onboarding')
     from wiki.document_version
    where document_id = wiki_test.doc('root.engineering.guides.onboarding')
      and upper_inf(valid)));

select wiki_test.expect('there is a GIN index behind it',
  (select count(*) > 0 from pg_index i
     join pg_class c on c.oid = i.indexrelid
     join pg_am am on am.oid = c.relam
    where c.relname = 'document_version_search_idx' and am.amname = 'gin'));
