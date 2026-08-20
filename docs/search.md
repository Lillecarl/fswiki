# Search

Full text, in Postgres, filtered by the same ACL as everything else.

There is one interesting decision in here and it is not the tokeniser. It is
that a text index and a row-level security policy want opposite things from
the planner, and the planner resolves that in favour of security every time —
correctly, and at a cost that makes the obvious implementation unusable.

## The index is a generated column

`document_version.search` is a stored generated `tsvector` with a GIN index
over it. A revision's content never changes, so the column is never stale by
construction: no trigger to forget, no queue to drain, no window in which a
published page is unfindable.

```sql
to_tsvector('english',
  replace(replace(path::text, '.', ' '), '-', ' ') || ' ' ||
  left(coalesce(content, ''), 262144))
```

**The source is indexed, not rendered text**, and that was measured before it
was chosen. `to_tsvector` splits on non-word characters, so nearly all markup
falls out by itself:

    **bold** `code` [a link](http://x.y) ~~struck~~ .. note::
    -> 'bold' 'code' 'link' 'x.y' 'struck' 'note' 'direct'

The residue is directive names, fence languages and URL hosts. The alternative
is extracting plain text, and the question that kills it is *where*. Rendering
happens in the client, so a stored plain-text column would be computed by
whichever client is writing — the CLI, the FUSE driver, anything hand-rolled —
and the database could not check any of them. That is a second source of truth
for what a page says. An indexed `python` from a code fence is the cheaper
mistake.

The path is folded in beside the content so a page is findable by its own
name. `document.title` is not: a generated column may only read its own row,
and titles are the slug prettified almost everywhere.

`english` is hard-coded. That is a limitation rather than a decision — a wiki
with German pages wants a `regconfig` column and therefore a later file in
`schema/tables/`. Nothing has to move for that: the column is generated, so
changing the expression rebuilds it.

### The cap is not tuning

`to_tsvector` raises above 1,048,575 bytes, and a generated column computes on
INSERT. Without a cap, pasting a large log into a page makes the **push** fail
with an error about a text-search type, which is an absurd reason to lose
somebody's work.

Measured on PostgreSQL 18 with the densest input there is, all-distinct short
words:

| source characters | tsvector |
| --- | --- |
| 262,144 | 563 KB |
| 524,288 | 1,125 KB — over the limit |

So the cap is 256 Ki characters, at 54% of the ceiling. Past it a page still
publishes, still renders, and is still findable by anything in its first
quarter-megabyte. `left()` counts characters rather than bytes, which is the
safe direction: a multi-byte character can only produce fewer lexemes per
character, never more.

## Why `wiki.search` is SECURITY DEFINER

The obvious query needs none of this. Join `document_version` to `document`,
test `search @@ query`, and let `document_version_select` filter the rows. It
is four lines and it is unusable.

Measured on 5,052 documents, 5,010 published revisions of about 950 B each, as
a reader who may see half of them:

| arm | ms |
| --- | --- |
| the obvious query, through RLS | 1,775 |
| the same query as superuser, no RLS | 1 |
| `count(*) from wiki.document` (the `can_ctx` policy) | 106 |
| `count(*) from wiki.document_version` (the `has_capability` policy) | 1,685 |

The text search is one millisecond. The ACL is the other 1,774.

`EXPLAIN` says why, and it is not a planner accident:

```
Bitmap Heap Scan on document_version v (actual time=12.090..1726.320 rows=5000)
  Recheck Cond: upper_inf(valid)
  Filter: (wiki.has_capability(document_id, 'read') AND (search @@ '...'::tsquery))
  Rows Removed by Filter: 10
  ->  Bitmap Index Scan on document_version_current_idx
```

The GIN index is not in the plan at all. `ts_match_vq`, which implements `@@`,
has `proleakproof = false`, and PostgreSQL will not evaluate a non-leakproof
user qualifier ahead of a security qualifier — a leaky one could report
something about a row the caller may not see. So the RLS test runs first, on
every row, and `@@` can never become an index condition.

**The ordering is the whole problem, and a query cannot ask for a different
one.** So the function takes it. `wiki.search` runs as the owner, which puts
the index back in charge, and then applies `wiki.can_ctx` to the survivors
itself.

| query | matches | ms |
| --- | --- | --- |
| one page | 1 | 2 |
| fifty pages | 50 | 17 |
| every page in the wiki | 5,000 | 151 |

The last row is the honest ceiling. A closed-world ACL cannot know the top
twenty readable results without asking about every match, and a query that
matches the whole wiki asks about the whole wiki.

### What it costs, and what pays for it

The ACL is now written twice, and a copy that drifts from its original is a
page leaking to someone the policy would have refused. So the predicate is
deliberately the same one `document_version_select` uses — `read` on the
owning document, with **no traversal arm**, so a folder you may only pass
through still yields no content.

That equivalence is not a comment. `server/test/100_search_test.sql` runs
seven fixture users against thirteen words and compares the function to
`wiki.current_document` in both directions of disagreement. A missing row is a
bug; an extra row is a disclosure.

### The InitPlan, one level up

The context is written `(select wiki.acl_context('read'))` and that is not
interchangeable with the CTE it replaced. Written as a CTE the planner inlines
it and the reader's whole ACL is rebuilt once per candidate row.

| form | ms, on the query that matches everything |
| --- | --- |
| `with ctx as (select wiki.acl_context('read')) … cross join ctx` | 1,500 |
| `(select wiki.acl_context('read'))` | 151 |

Same rows out. It is the same trap `document_select` documents, found again in
a different shape — see issue #10.

## Drafts need none of that

`wiki.search_drafts` is SECURITY INVOKER and stays that way. `draft_all`
restricts every draft to its author, which is an index lookup rather than an
ACL walk, so there is no ordering problem to solve and nothing to gain by
bypassing anything. The risky construction above exists because a measurement
demanded it, and it should not spread by imitation — there is an assertion
that `wiki.search` is the only SECURITY DEFINER function of the two.

There is no stored vector on `draft` either. A draft is one author's handful
of mutable rows. The expression is computed at query time and is the same one
the generated column uses, so a word matches a draft exactly as it matches the
page it will become.

`Pages(drafts=True)` — the preview — folds them in and lets a draft win over
the published copy of the same path, because a draft is what its author sees
in the mount, in `fswiki status` and on the page itself. Search disagreeing
would be search being wrong. The server never asks.

## Both functions are `volatile`, and it is about transport

Nothing in either writes. PostgREST runs a `stable` function in a read-only
transaction, and impersonation refuses any transaction it cannot write its own
log into — so a `stable` search would have been unreachable to exactly the
caller who most needs their view of the wiki to match somebody else's.

The alternative is a volatile twin beside a stable one, which is what
`changed()` and `acting_as()` are. One endpoint that always works is the
better bargain for a function this new.

## The excerpt is the one place document text becomes markup

`ts_headline` does not escape. Its output is one person's document with
markers in it, being shown to another person — so markers that were already
HTML would arrive at a browser as markup somebody else wrote.

They are **STX and ETX**, two control characters no HTML parser will ever act
on. `pages.excerpt_html` escapes everything around them and only then turns
them into `<mark>`. It is a small state machine rather than two `replace`
calls, so the output is balanced whatever the input was: an author who types a
STX into a page would otherwise open a highlight that never closes and tint
the rest of the results list. The worst they achieve is a highlight in their
own excerpt, which is what they asked for.

## Existence stays secret

The route answers `Nothing here matches that.` — a sentence, not a count.

"No results for a word nobody wrote" and "no results for a word that is only
in a page you may not open" have to be the same response. A count of hidden
matches would turn the search box into an oracle over the whole wiki, one word
at a time. `test/test_search.py` compares the two responses below the search
box, because the box echoes the query back and the query is the one thing that
legitimately differs.

Timing is not addressed and is not claimed to be. A query that matches many
hidden pages costs more ACL tests than one that matches none, and that
difference is observable to somebody who measures carefully. Closing it would
mean paying the worst case on every request; the disclosure is a rate, not a
value, and this is a wiki.

## What it does not do yet

- **One language.** See the `regconfig` column above.
- **No phrase or field operators beyond `websearch_to_tsquery`**, which is
  what a person types: bare words, `"a phrase"`, `-excluded`, `or`.
- **No paging.** The limit is capped at 100 because every row costs an ACL
  test. Paging under a closed-world ACL means a stable sort key and an offset
  the server can trust, which is a design rather than a parameter.
- **No title column in the vector.** The path covers most of it. A title that
  differs from its slug is not findable by its title.
