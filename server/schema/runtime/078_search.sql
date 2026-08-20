-- Search, and the one place in this schema where a policy is applied by hand.
--
-- Everything else here lets RLS do the filtering. This file does not, and the
-- reason is a measurement rather than a preference. Read it before changing
-- anything below.
--
------------------------------------------------------------------------------
-- Why this is SECURITY DEFINER
------------------------------------------------------------------------------
--
-- The obvious version is four lines and needs none of this: join
-- `document_version` to `document`, test `search @@ query`, let
-- `document_version_select` filter the rows. It is also unusable.
--
-- Measured on a 5,052-document wiki, 5,010 published revisions of about 950 B
-- each, as a reader who may see half of them:
--
--   | arm                                            |     ms |
--   | ---------------------------------------------- | ------ |
--   | the obvious query, through RLS                 |  1,775 |
--   | the same query as superuser, no RLS            |      1 |
--   | `count(*) from wiki.document` (can_ctx policy) |    106 |
--   | `count(*) from wiki.document_version`          |  1,685 |
--
-- So the text search is one millisecond and the ACL is the other 1,774.
--
-- `EXPLAIN` says why, and it is not a planner accident. `ts_match_vq`, which
-- implements `@@`, has `proleakproof = false`. PostgreSQL will not evaluate a
-- non-leakproof user qualifier before a security qualifier, because a
-- leaky one could report something about a row the caller may not see. So the
-- RLS test runs first, on **every** row, and the GIN index is never used as an
-- index condition — the plan falls back to `document_version_current_idx` and
-- filters 5,010 rows through an ACL walk to find 50.
--
-- The ordering is the whole problem, and the ordering is not something a query
-- can ask for. So this function takes it: it runs as the owner, which puts the
-- index back in charge, and then applies `wiki.can_ctx` to the survivors
-- itself. **The predicate is the same one `document_version_select` uses** --
-- `read` on the owning document, no traversal arm, so a folder you may only
-- pass through still yields no content. That equivalence is not asserted by
-- reading this comment: `server/test/100_search_test.sql` compares this
-- function against the RLS view over the cross product of every fixture user
-- and every document, in both directions of disagreement.
--
-- The result: 17 ms for a query matching 50 pages, 151 ms for one matching all
-- 5,000. Which is the honest ceiling — a closed-world ACL cannot know the top
-- twenty readable results without asking about every match, and a query that
-- matches the whole wiki asks about the whole wiki.
--
------------------------------------------------------------------------------
-- The InitPlan, again
------------------------------------------------------------------------------
--
-- `(select wiki.acl_context('read'))` is not a stylistic choice and it is not
-- interchangeable with the CTE it replaced. Written as a CTE the planner
-- inlines it and the caller's ACL is rebuilt once per candidate row; written
-- as an uncorrelated scalar sub-select it becomes an InitPlan, evaluated once
-- per statement. Measured here, on the query that matches everything:
-- **1,500 ms as a CTE against 151 ms as an InitPlan**, same rows out. The same
-- trap `document_select` documents, found again a level up. See issue #10.

create or replace function wiki.search(p_query text, p_limit integer default 20)
returns table (
  document_id  uuid,
  path         ltree,
  title        text,
  version      integer,
  content_type text,
  rank         real,
  excerpt      text
)
-- `volatile` rather than `stable`, and it is a transport decision rather than
-- a claim about the body -- nothing here writes. PostgREST runs a stable
-- function in a read-only transaction, and impersonation refuses any
-- transaction it cannot write its own log into, so a stable `search` would be
-- unreachable to exactly the caller who most needs their view of the wiki to
-- match somebody else's. The alternative is a volatile twin beside a stable
-- one, which is what `changed()` and `acting_as()` are; one endpoint that
-- always works is the better bargain for a function this new.
language sql volatile security definer parallel safe
set search_path = wiki, public, pg_temp
-- `websearch_to_tsquery` raises a NOTICE when a query holds no lexemes, which
-- is what "" and "the the the" both do. The function already answers those
-- with no rows; the notice would reach the client as a warning about a
-- text-search internal, which tells a person nothing they can act on.
set client_min_messages = warning
as $$
  -- Two passes on purpose. The inner one ranks and cuts; the outer one builds
  -- the excerpt, because ts_headline re-parses the whole document and a target
  -- list is evaluated before the sort -- so computing it inside would run it
  -- on every match rather than on the twenty that survive.
  with hit as (
    select d.id, d.path, d.title, v.version, v.content_type, v.content,
           ts_rank(v.search,
                   websearch_to_tsquery('english', coalesce(p_query, ''))) as rank
      from wiki.document_version v
      join wiki.document d on d.id = v.document_id
     where v.search @@ websearch_to_tsquery('english', coalesce(p_query, ''))
       and upper_inf(v.valid)
       and not v.is_tombstone
       -- The policy, by hand. See the header.
       and wiki.can_ctx(d.path, d.is_folder, d.owner_id, 'read',
                        (select wiki.acl_context('read')))
     order by rank desc, d.path
     -- A caller sets the page size; a caller does not get to ask for the whole
     -- wiki in one request, because every row costs an ACL test.
     limit least(greatest(coalesce(p_limit, 20), 1), 100)
  )
  select hit.id, hit.path, hit.title, hit.version, hit.content_type, hit.rank,
         -- Delimited with STX and ETX rather than with tags. ts_headline does
         -- not escape, so its output is document text with markers in it, and
         -- markers that are already HTML would arrive at a browser as HTML
         -- somebody else wrote. Two control characters cannot be mistaken for
         -- markup by anything, and the client escapes the text before turning
         -- them into a tag. The worst an author can do by typing one is put a
         -- highlight in their own excerpt.
         ts_headline('english', coalesce(hit.content, ''),
                     websearch_to_tsquery('english', coalesce(p_query, '')),
                     'StartSel=' || chr(2) || ',StopSel=' || chr(3) ||
                     ',MaxWords=28,MinWords=12,ShortWord=2')
    from hit
   order by hit.rank desc, hit.path;
$$;

comment on function wiki.search(text, integer) is
  'Full-text search over published content, filtered by the caller''s own '
  '`read`. SECURITY DEFINER so the GIN index runs before the ACL rather than '
  'after it; the ACL is then applied by hand and tested against the RLS view.';

------------------------------------------------------------------------------
-- Drafts, which need none of the above
------------------------------------------------------------------------------
--
-- SECURITY INVOKER, and deliberately so. `draft_all` restricts every draft to
-- its author, which is an index lookup rather than an ACL walk, so there is no
-- ordering problem to solve and nothing to gain by bypassing anything. The
-- risky construction above exists because a measurement demanded it, and it
-- should not spread to a place that never asked for it.
--
-- No stored tsvector either. A draft is one author's handful of mutable rows;
-- an index on it would buy nothing and would need maintaining. The expression
-- is the same one the generated column uses, so a word matches a draft exactly
-- as it matches the page it will become.
create or replace function wiki.search_drafts(p_query text,
                                              p_limit integer default 20)
returns table (
  document_id  uuid,
  path         ltree,
  content_type text,
  rank         real,
  excerpt      text
)
-- Volatile for the same transport reason as wiki.search() above.
language sql volatile parallel safe
set search_path = wiki, public, pg_temp
set client_min_messages = warning
as $$
  with hit as (
    select dr.document_id, dr.path, dr.content_type, dr.content,
           ts_rank(to_tsvector('english',
                     replace(replace(dr.path::text, '.', ' '), '-', ' ') || ' ' ||
                     left(coalesce(dr.content, ''), 262144)),
                   websearch_to_tsquery('english', coalesce(p_query, ''))) as rank
      from wiki.draft dr
     where dr.content is not null
       and to_tsvector('english',
             replace(replace(dr.path::text, '.', ' '), '-', ' ') || ' ' ||
             left(coalesce(dr.content, ''), 262144))
           @@ websearch_to_tsquery('english', coalesce(p_query, ''))
     order by rank desc, dr.path
     limit least(greatest(coalesce(p_limit, 20), 1), 100)
  )
  select hit.document_id, hit.path, hit.content_type, hit.rank,
         ts_headline('english', coalesce(hit.content, ''),
                     websearch_to_tsquery('english', coalesce(p_query, '')),
                     'StartSel=' || chr(2) || ',StopSel=' || chr(3) ||
                     ',MaxWords=28,MinWords=12,ShortWord=2')
    from hit
   order by hit.rank desc, hit.path;
$$;

comment on function wiki.search_drafts(text, integer) is
  'The caller''s own unpublished drafts, searched. SECURITY INVOKER: draft RLS '
  'already restricts every row to its author.';

------------------------------------------------------------------------------
-- Grants, beside the definition rather than in 060_roles.sql
------------------------------------------------------------------------------
--
-- Same reason wiki.change_token() is granted in 075_changes.sql: a function
-- defined after that file cannot be named in it. 070_public_test.sql asserts
-- the anonymous surface exactly, so `search` appearing there is a deliberate
-- edit in two places.
--
-- Anonymous callers get `search` and not `search_drafts`. An anonymous caller
-- has no account, so `wiki.current_user_id()` is null and `draft_all` admits
-- nothing -- the grant would be harmless and it would also be a lie about the
-- surface, so it is not made.
grant execute on function wiki.search(text, integer)
  to fswiki_user, fswiki_anon;

grant execute on function wiki.search_drafts(text, integer)
  to fswiki_user;
