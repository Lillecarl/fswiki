-- Full-text search: one generated column and one index.
--
-- The index lives on `document_version` rather than on a table of its own,
-- because a revision's content never changes. A generated column is therefore
-- never stale by construction — there is no trigger to forget, no queue to
-- drain, and no window in which a published page is unfindable.
--
-- What is indexed is the **source**, not rendered text, and that was measured
-- before it was chosen. `to_tsvector` splits on non-word characters, so almost
-- all markup falls out by itself:
--
--   **bold** `code` [a link](http://x.y) ~~struck~~ .. note::
--   -> 'bold' 'code' 'link' 'x.y' 'struck' 'note' 'direct'
--
-- The residue is directive names, fence languages and URL hosts. That is a
-- little noise in exchange for no extraction step: rendering to plain text
-- would have to happen somewhere, and "somewhere" means every client that can
-- write — the CLI, the FUSE driver and any hand-rolled one — each computing a
-- derived column the database cannot check. A second source of truth for what
-- a page says is a worse bargain than an indexed `python` from a code fence.
--
-- The path is folded in beside the content so that a page is findable by its
-- own name. Dots and hyphens become spaces first, so `root.engineering.guides
-- .onboarding-v2` yields `engineering`, `guides`, `onboarding` and `v2`.
-- `document.title` is deliberately absent: it lives on the other table, and a
-- generated column may only read its own row. Titles are the slug prettified
-- almost everywhere, so the path covers the same ground.
--
-- `english` is hard-coded, and that is a limitation rather than a decision.
-- A wiki with German pages wants a per-document configuration, which is a
-- `regconfig` column on this table and therefore a later file in this
-- directory. Nothing here has to move for that to happen: the column is
-- generated, so changing the expression rebuilds it.

-- The cap is not tuning; it is what stops search from breaking publishing.
--
-- `to_tsvector` raises when the result exceeds 1,048,575 bytes, and a
-- generated column computes on INSERT — so without a cap, pasting a large log
-- into a page makes the push fail with an error about a text-search type,
-- which is an absurd reason to lose somebody's work. Measured on PostgreSQL
-- 18 with the densest input there is, all-distinct short words: 262,144
-- characters produce a 563 KB tsvector, and 524,288 produce 1,125 KB, which
-- is over the limit. So the cap is 256 Ki characters, at 54% of the ceiling.
--
-- Past it a page still publishes, still renders and is still findable by
-- anything in its first quarter-megabyte. `left()` counts characters rather
-- than bytes, which is the safe direction: a multi-byte character can only
-- produce fewer lexemes per character, never more.
alter table wiki.document_version
  add column search tsvector
  generated always as (
    to_tsvector(
      'english',
      replace(replace(path::text, '.', ' '), '-', ' ') || ' ' ||
      left(coalesce(content, ''), 262144))
  ) stored;

comment on column wiki.document_version.search is
  'Path and content as lexemes, for wiki.search(). Generated, so it cannot go '
  'stale; capped at 256 Ki characters so a huge page cannot fail its own push.';

create index document_version_search_idx
  on wiki.document_version using gin (search);
