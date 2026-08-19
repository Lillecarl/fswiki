# Rendering

Turning a document into something to look at, on a laptop and on a server.

All numbers below are measured on this host against markdown-it-py 4.2.0,
mistune 3.3.3, python-markdown 3.10.2 and nh3 0.3.2.

## The thing that makes this wiki different

Every other markdown wiki can be a static site generator. This one cannot, and
it is worth being exact about why, because the reason also says what to build
instead.

An SSG produces **one artifact for everyone**. fswiki's permissions are per
document and per principal, enforced by RLS on every read. Those two are not
compatible, and the ways round it are all worse than the thing they avoid:

- **A site per principal.** The set of pages a person can see is the ACL
  evaluated over the whole tree; generating one site each is a rebuild per
  person per change, and it goes wrong quietly — a rebuild that runs late
  serves a page to someone who lost access an hour ago.
- **Static files behind an authorising proxy.** The proxy has to map a URL to a
  document and ask whether this caller may read it, which means the proxy needs
  the ACL. Now the permission model exists in two places, and the second copy is
  the one that will drift.

So there is no SSG here. What we want is the *performance* of one without a
second permission model, and that is a cache.

**Staticness is a property of the ACL, not of the renderer.** The documents an
anonymous reader may read are exactly the set that can be published as flat
files, because for them "one artifact for everyone" is true. A public wiki can
have a static front end for its public part and lose nothing. That is an
optimisation for later, and it falls out of the design below rather than
competing with it.

## What rendering costs

The question that matters is whether to render once per revision and store the
result, or render on every read. That is only a real question if rendering is
expensive.

| | median | notes |
| --- | --- | --- |
| a typical fixture page (~800 B) | **0.62 ms** | the fixtures run 22 B – 430 B |
| this repo's `audit-trail.md` (14.7 kB) | **9.00 ms** | a long page, by wiki standards |
| sanitising the output (nh3) | 0.59 ms | |
| one code block, coloured | 0.19 ms | pygments, on this repo's largest block |
| one maths expression to MathML | 0.32 ms | `latex2mathml`, in-process |
| **a cache hit** | **2.55 µs** | 1.93 µs to build the key, 0.62 µs to look it up |

Roughly 0.6 ms per kB, against the ~30 ms a content fetch already costs over
HTTP (measured in [audit-trail.md](audit-trail.md)). A typical page is a couple
of per cent of the round trip; a very long one is a third of it.

Engines, on the same 5.4 kB input: markdown-it-py 6.26 ms, mistune 6.35 ms,
python-markdown 11.16 ms.

## Render on read, cache on the revision

**The cache key is immutable, and that is the whole argument.** A revision's
content never changes — that is what `document_version` means — so
`(document_id, version, renderer_version)` names one byte string forever. The
hard part of caching, knowing when to throw something away, does not exist
here. There is nothing to invalidate, only something to evict.

That makes render-on-read with a cache reach the same steady state as storing
HTML in the database, without any of what storing it costs:

- no column to migrate and no doubling of stored bytes;
- no worker, and so no window in which a page is published but not yet
  rendered;
- **a renderer upgrade is a cache flush.** With HTML in the table, upgrading
  markdown-it means either re-rendering every revision that ever existed or
  serving output from a renderer you no longer run.

Rendering in the *client* is the other option and it is the wrong one: the
client would then choose the HTML that other people's browsers execute. Stored
cross-site scripting is exactly the shape of that bug. If the server has to
sanitise client-supplied HTML anyway, it may as well render.

The rule this generalises to, for when it stops being just markdown:

> Cheap and deterministic → render on read and cache. Expensive or
> non-deterministic → materialise at publish and store it.

Markdown is the first. Diagrams, LaTeX and anything that shells out are the
second, and when they arrive they belong in a publish-time pipeline whose
output is stored, not in the read path.

## The split that makes the cache work

A rendered body is a function of the content and the renderer. It is the same
for every reader, which is what makes it cacheable.

Almost everything else on the page is a function of the *reader*: the
navigation tree, the breadcrumbs, whether an edit affordance appears, and —
importantly — which links are live.

If links are resolved to their targets during rendering, the fragment becomes
per-reader and the cache dies. So the renderer emits links in a **neutral
form** that names the target path and decides nothing:

    <a href="/-/fswiki/root.engineering.onboarding">Onboarding</a>

and a per-request pass over the anchors decides what each one becomes. A page
has tens of links; walking them is microseconds against a 0.6 ms render, so the
expensive half stays shared and the cheap half stays correct.

Cache the body. Compose the page.

## The link graph leaks, and it leaks at render time

This is the failure mode specific to fswiki, and it is not obvious.

A page you may read contains `[[engineering/secret-plans]]`. If that renders as
a live link with the target's title, the rendering has just told you that the
document exists, where it lives and what it is called. The ACL granted none of
that. The audit trail would record the click, but by then the disclosure has
already happened — it happened in the HTML.

So a link to a document the reader may not `read` renders as **plain text**.
And it renders as the same plain text as a link to a document that does not
exist at all: *"forbidden" and "missing" must be indistinguishable to the
reader*, because the difference between them is the disclosure. This is the
same reasoning that makes `push()` report a create collision as `forbidden`
rather than handing back the occupant's content.

The corollary is that link liveness cannot be baked into a shared cache entry,
which is why the neutral form above is not merely tidy. `render.links.resolve`
takes a callable that answers "what URL should this reader follow for this
path, if any", and `render.links.unresolved` counts anchors that never went
through it — a composed page has none, and serving a cached body without
composing it is the mistake worth catching automatically.

## Raw HTML is off

Markdown parsers can pass HTML through. Ours will not.

Wiki content is written by everyone who can write anywhere in the tree, and it
is read by everyone else. Raw HTML in a document is a script tag in every
reader's session, and the ACL has nothing to say about that — `write` on a page
is not meant to be `execute` on the reader.

Two layers, because the parser and the sanitiser fail differently:

1. `html: false` in the parser, so raw blocks never become HTML in the first
   place.
2. **nh3 over the output anyway** (0.59 ms). The parser not emitting raw HTML
   does not stop a `javascript:` href, a hostile `src`, or a future plugin from
   producing something unpleasant. Sanitising the output catches what the
   parser was never asked about.

## Pluggable backends

No engine is settled on, so none is baked in. The seam is deliberately narrow:

    [[wikilinks]]  ->  backend.to_html()  ->  sanitise

Only the middle step is pluggable. A backend converts markup to HTML and does
nothing else — no wiki links, no sanitising, no opinion about who may read
what. Everything specific to this wiki lives on either side of it, in code that
does not change when the backend does.

**The backend is pluggable precisely because the invariants are not.** The
link-graph leak above is a security property; a security property that each
backend has to reimplement is one that some backend will eventually get wrong.
Sanitising is not offered as a backend's choice for the same reason — a
document is written by one user and read by another, and which engine an
operator installed should not decide what a reader's browser executes.

A backend is three attributes and a function:

    class Backend(Protocol):
        name: str                      # stable; part of the cache key
        version: str                   # the library's, so an upgrade misses the cache
        content_types: tuple[str, ...]
        def to_html(self, text: str) -> str: ...

Selection is by **`content_type`**, which the schema already carries per
revision. So a second markup language is a registration and nothing else — no
new column, no branch anywhere in the read path. `$FSWIKI_RENDERER` pins a
particular engine for a deployment; an explicit argument beats both.

Nothing is a hard dependency. Each shipped backend registers itself only if its
library imports, so a build without them still gives a working client, and
`fswiki render --list-backends` says what this installation actually has:

    $ fswiki render --list-backends
      docutils         0.23       text/x-rst
      markdown-it-py   4.2.0      text/markdown
      mistune          3.3.3      text/markdown
      plain            1          text/plain

### reStructuredText costs more than markdown, and one setting more than that

docutils renders a 536 B page in **5.14 ms**, against 0.62 ms for markdown-it
on a comparable one — about eight times the price, which is a few per cent of a
request that already spends ~70 ms talking to PostgREST. That is the whole of
the performance story and it is not interesting.

The interesting part is that **docutils is unsafe by default for this use**,
and two of the three settings that fix it were measured rather than reasoned
about:

- `.. include:: /any/path` opens that file and puts its contents in the page.
  Arbitrary server-file disclosure, written by one user and read by another —
  and **the sanitiser does not catch it**, because the contents arrive as text
  nodes rather than as tags. `file_insertion_enabled=False`.
- `.. raw:: html` injects markup. nh3 does strip the `<script>` it produces, so
  this one is a layer rather than the wall. `raw_enabled=False`.
- A **`docutils.conf` in the working directory overrides `settings_overrides`**,
  so the two above can be set and mean nothing. This is the one nobody would
  guess. `_disable_config=True`.

All three are asserted in `test/test_render_rst.py`, including a test that the
sanitiser would *not* have saved us — so that nobody later decides the parser
setting is redundant because "nh3 handles it".

The sanitiser gained `<section>` and `<aside>` for this. Both are inert, and
without them nh3 unwraps every `.. note::` into an undistinguished paragraph,
which loses the one thing an admonition is for. That changes what the pipeline
emits, so `PIPELINE_VERSION` went to 2 — which is exactly what that number is
for.

`markdown-it-py` is preferred where present, because CommonMark is a
specification rather than a dialect and the filesystem is the source of truth —
files here get edited by tools that know nothing about us, and a document should
mean the same thing to all of them. There is also a markdown-it in JavaScript,
which is the only way a future browser-side live preview agrees with the server
without shipping two dialects. `mistune` is within 2 % on speed and ships as the
second opinion. `plain` needs no library at all.

### Maths, and the subprocess it does not need

`$e^{i\pi}+1=0$` renders to MathML in **0.32 ms**, in this process. There is
no TeX, no scratch directory, no timeout and no concurrency cap, and the reason
is a distinction worth stating plainly: **maths is not a LaTeX document.**

`latex2mathml` is a converter rather than an interpreter. It reads the notation
and writes MathML, and it has no filesystem and no subprocess to reach for.
Measured against the two things a real TeX run would hand an author:

| written | what happens |
| --- | --- |
| `\input{/etc/hostname}` | 260 B of MathML; the hostname is not in it |
| `\write18{touch /tmp/x}` | 291 B of MathML; nothing runs |
| `\def\x{\x}\x` | `RecursionError` in 0.5 ms — caught |

That third row is the only failure mode, and it is the interesting one: it
*raises*, which a TeX process in a `\loop` would not. Issue #7 has the
measurements for the other route — `openin_any` defaults to `a`, so a document
reads any file the process can — and none of it is needed here.

Three things can go wrong: the converter is absent, the expression is
malformed, or the expression is pathological. All three show the LaTeX source
instead, in the shape docutils already uses for maths it cannot convert, so the
markdown path and the reStructuredText path degrade the same way.

reStructuredText needs none of this. docutils converts `:math:` and `.. math::`
itself, so `math_output` is pinned to `mathml` rather than left to a default
that has already moved once.

**The sanitiser was the whole job.** MathML is *foreign content* to the HTML
parser, which means an element nh3 does not know is dropped **whole** — children
and text with it — rather than unwrapped. Measured before the allowlist existed:
206 bytes of MathML in, **0 bytes out**, not even the numbers. So MathML is
listed explicitly, and `PIPELINE_VERSION` went to 4.

The list is the union of what the two converters emit, and two things are left
out on purpose:

- **`annotation-xml`.** With `encoding="text/html"` it becomes an HTML
  integration point, which is the classic mutation-XSS route through a
  sanitiser that allows MathML. Left out, nh3 drops it and the `<script>`
  inside it together.
- **`href`, on every element.** `\href{...}{...}` puts one on an `<mrow>`, and
  latex2mathml will write `<mrow href="javascript:alert(1)">` without complaint.
  Maths is notation, not navigation.

That is why this was a smaller decision than allowing SVG. MathML has no
scripting element, nothing that navigates, and no `foreignObject` to smuggle
HTML through.

### Syntax highlighting, and the stylesheet nobody mentions

Code blocks are coloured by **pygments**, in this process, behind one function
that never raises. A block that cannot be coloured is a plain block: an unknown
language, an absent pygments, a block over the cap and a lexer that throws are
four reasons and one behaviour.

**The sanitiser needed no change at all.** That is the whole difference from
maths. Highlighting emits `span` carrying `class`, and both were already
allowed, so `PIPELINE_VERSION` stays where it was. What moves the cache key
instead is `highlight.version()` in every backend's options — because a
deployment with pygments and one without produce different bytes for the same
revision, and they must not share a key.

Two decisions are worth stating, and both are about inputs written by one user
and read by another:

- **The language comes from the fence and from nowhere else.** `guess_lexer`
  took **293 ms** on hostile input to conclude "Text only", and a wrong guess is
  worse than no colour. It is never called.
- **One block is capped at 4 kB, and one page at 32 kB.** The cost per byte
  varies by an order of magnitude with what a block holds, so the cap bounds
  the constant rather than the order:

| 4 kB of | cost |
| --- | --- |
| ordinary Python (1.3 µs/B) | 3.7 ms |
| solid punctuation (10 µs/B) | 39.6 ms |

4 kB is eleven times the largest code block in this repository's own
documentation, whose median block is 158 B.

**The block cap bounds a block and says nothing about a page**, which took a
second measurement to notice. Nothing limits how many blocks a document has and
`document_version.content` is a bare `text`, so 200 blocks at the cap is 822 kB
of source and **8.7 seconds** of render — 99% of it highlighting, against 96 ms
for the same page in a language pygments does not know. That is an 85×
amplification from content one user writes and another reads, and the render
cache does not help: every revision is a new key and so is every new document.

So a page has a budget too, and once it is spent the remaining blocks render
plain. The same 200-block page now renders in 428 ms, flat in the number of
blocks.

**Bytes rather than time, and that is the design rather than the easy option.**
A deadline was measured and it works: the longest uninterruptible step is
0.24 ms at the block cap — everything expensive is many cheap tokens, and
everything uninterruptible is one big token and cheap — and a check between
tokens costs nothing outside inputs with tens of thousands of them. It is still
wrong here, because the render cache stores one body per
`(document_id, version, renderer)` and nothing in that key says how busy the
server was. A page would come back coloured on a quiet server and plain on a
busy one, and whichever ran first is what every later reader gets. A byte
budget depends only on the content and the order of its blocks, so every
machine caches the same bytes. Issue #12 has the measurements.

Both limits go in each backend's `options`, beside the highlighter version, for
the reason the version is there: they decide what a page comes back holding, so
the cache key has to move with them. `FSWIKI_HIGHLIGHT_BLOCK_BYTES` and
`FSWIKI_HIGHLIGHT_PAGE_BYTES` move them, once, at import — a limit that could
change afterwards would change what a page holds without changing the key it is
cached under. Either set to `0` turns highlighting off, which is a value rather
than a special case: every block is then over the limit.

**Both markdown engines colour the same fence the same way**, byte for byte,
because `render.highlight.block` writes the wrapper once for both. docutils
does its own highlighting, and `syntax_highlight` is set to `short` rather than
`long` for one reason: the short names are the ones pygments' own HTML
formatter emits, so the two paths need one stylesheet between them rather than
two. It degrades by itself — docutils raises `LexerError` when pygments is
absent or the language is unknown, and re-lexes without colour because
`report_level` is above 2.

**The cost nobody mentions is the stylesheet.** Class-based output means
nothing without CSS, and these pages render in both schemes.
`get_style_defs()` for two schemes is **8,853 B**, against a page stylesheet
that was 3,579 B — pygments' own would have made it three and a half times the
size. The rules in `fswiki_core.pages` are **2,168 B** and make it one and a
half: the classes twelve languages actually emit, grouped by what they
mean rather than by which lexer produced them, with punctuation, whitespace and
plain names left the colour of the surrounding text. A class with no rule is a
token nobody notices, so `test_render_highlight.py` fails when a language emits
one that is neither styled nor deliberately plain.

**tree-sitter was measured against it, and lost.** It is the better engine for
an editor and it is the wrong one here, which took building the whole thing to
find out: parsers from `tree-sitter-language-pack`, `highlights.scm` from
`tree-sitter-grammars`, and a capture-name mapping written by hand. All eleven
grammars sampled compiled their queries, so the packaging was the smaller half
of the problem.

Three numbers decided it. **A parse costs about 0.18 ms whatever the input**,
so on this repo's median 158 B block pygments answers in 0.09 ms and
tree-sitter takes 0.40 ms — parsing alone is already twice the whole pygments
call. tree-sitter wins 1.4×–3.9× from 1 kB up, which is where a render cache
means the block is coloured once per revision anyway. And it classifies **6
kinds of token against pygments' 18** on the same Python: `Cache`, `__init__`,
`put`, `self`, `int` and `print` all come back as "a name". Closing that gap
means writing queries per language rather than writing a map, and the grammars
do not even share a capture vocabulary — sql says `conditional`, diff says
`diff.plus`, yaml says `property`.

The engine is behind one function, so this stays a swap rather than a rewrite
if the argument changes. Issue #9 has the full comparison.

### The renderer is part of the cache key

`Rendered.renderer` identifies the whole pipeline, not just the engine:

    markdown-it-py/4.2.0+cfg91b4fb2e+fswiki4

That goes in the cache key beside `(document_id, version)`. Leave it out and
switching engines — or upgrading one — quietly serves output that the code now
running would not produce. The `+fswiki<n>` suffix is the pre- and post-passes'
own version, because they affect the bytes too.

### The cache exists, and it is not the bottleneck

`fswiki_core.render.cache` is a byte-bounded LRU on
`(document_id, version, renderer)`, held per process and consulted by `Pages`.
`FSWIKI_RENDER_CACHE_BYTES` sizes it; `0` turns it off.

Two rules make it correct rather than merely fast, and both are asserted in
`test/test_render_cache.py`:

- **What is stored is the neutral body**, with wiki links still under the
  reserved prefix. Resolution runs per reader afterwards. Cache the composed
  page instead and one reader's link graph is served to the next. A test
  composes one stored body for two readers with different outlines and
  requires the same bytes to resolve differently.
- **A draft is never stored.** Its content is mutable and it has no version, so
  the key does not exist for it. This is structural rather than conditional:
  there is no published row in the draft branch to build a key from.

Then the measurement, taken end to end against a live stack on this host:

| | median |
| --- | --- |
| `client.document()` — one PostgREST round trip | **77.9 ms** |
| `client.outline()` — the other | **89.5 ms** |
| rendering a 17 B page | 0.12 ms |
| rendering the 14.7 kB page | 9.90 ms |
| a cache hit | 2.55 µs |
| **a whole page, end to end** | **~168 ms** |

So the cache turns 9.90 ms into 2.55 µs — a factor of about 3,900 on the step
it covers — and **that step is six per cent of the page**. The two reads are
167 ms of the 168. Switching the cache off and on again moves the end-to-end
number by less than the noise.

The 167 ms was not transport. PostgREST answers a read of a small RLS'd table
in **1.3 ms**, and the query shape it generates is within the noise of a plain
`select`. The cost was inside the database, in `wiki.ace_covers()` — two
recursive CTEs over 22 rows of role and capability tables, run about fourteen
times per document, on every document, on every request.

That closure is 75 rows. It is now a table, rebuilt by
`seed/950_ace_closure.sql` and by a trigger on each of the four tables it is
derived from. Measured over HTTP, same stack, same 19 documents:

| | before | after | |
| --- | --- | --- | --- |
| the outline read | 41.3 ms | **11.1 ms** | 3.7× |
| one document read | 30.9 ms | **9.5 ms** | 3.2× |
| the full manifest, with capabilities | 410 ms | **91.2 ms** | 4.5× |
| a small RLS'd table, for the floor | 1.29 ms | 1.55 ms | — |

So a page's two reads went from ~168 ms to ~21 ms, and rendering — 0.12 ms for
a small page, 9.90 ms for a long one — stopped being a rounding error. The
cache earns its keep at the long end now.

What that did not change is the shape: `wiki.can()` was still called once per
document by `document_select`, so reading the tree stayed linear in the
documents visible — 1,326 µs per document before the closure and 246 µs after,
flat at every size measured. A thousand-document wiki was 254 ms per tree read
and a page does two of them.

**That shape is what changed next.** Nothing `wiki.can()` derives per row
depends on the row: which principals the caller counts as, which ACEs name
them, and which of those speak to `read` are all properties of the *statement*.
So they are derived once, into a `wiki.acl_context`, and every row is answered
from it. The mechanism is one line of SQL and it is easy to lose:

```sql
wiki.can_ctx(path, is_folder, owner_id, 'read', (select wiki.acl_context('read')))
```

`(select f())` with nothing correlated inside it is an **InitPlan**, which
PostgreSQL evaluates once per statement. Written as a bare
`wiki.acl_context('read')` it is a per-row call and gives back nine tenths of
the win — measured: 257 µs to build a context, against 16 µs to answer from
one. `EXPLAIN` is where you check the InitPlan is still there.

| documents | before | after | |
| --- | --- | --- | --- |
| 22 | 13.4 ms | **2.6 ms** | 5.2× |
| 175 | 42.5 ms | **5.7 ms** | 7.5× |
| 991 | 234.1 ms | **16.6 ms** | 14.1× |
| 3,031 | 669.7 ms | **42.0 ms** | 15.9× |

Per document that is 445 µs down to 13.8 µs. It is still linear — the policy
still has to answer for every row — but the constant is 27× smaller and the
answer is now a loop over a handful of array entries rather than a walk up the
tree with a join at every step.

The same InitPlan fixed two more reads that were not policies at all.
`wiki.current_document` exposes a `capabilities` column, which is eight ACL
questions per row and was the most expensive thing the mount asks for;
`wiki.syncable_document` asked by document id, which resolves to the path it
already had in hand. Over HTTP, same stack, the 19 fixture documents:

| | original | with the closure | now |
| --- | --- | --- | --- |
| the outline read | 41.3 ms | 11.1 ms | **3.3 ms** |
| the manifest, with capabilities | 410 ms | 91.2 ms | **9.1 ms** |
| the syncable tree | — | 11.4 ms | **8.4 ms** |

**Traversal was the other half.** A folder is visible when it holds something
the caller may read, and the old rule scanned the subtree asking about every
descendant — so a folder the caller may *not* read cost the whole tree under
it. On 3,720 documents of which one reader could see 122, that was 196 ms.

It is avoidable because a document is never allowed by accident: for an ACE to
apply to it, the ACE sits on one of its ancestors. So only descendants that are
also under one of the caller's own allow ACEs can come back true, and the scan
is driven from those ACEs — GiST is asked for the intersection rather than for
the subtree. The same case is now 82 ms, and the per-folder cost stopped
growing with what is under the folder: holding the folder count fixed and
quadrupling each subtree, 133 documents and 493 documents both cost 25 ms.

One case does not fit that argument and had to be added back. A document's
owner keeps `grant` whatever the ACL says — that is the escape from a deny that
locked everybody out — so an owned document is reachable with no allow ACE
above it anywhere. Leaving it out cost six disagreements in
`090_context_test.sql`, which is the reason that file compares every capability
rather than the two the policies happen to ask about.

`wiki.document_version` is the one left at the old cost, and deliberately. A
version row names its document by id, so a context-taking function would have
to resolve that id — and a caller can invent a context, which turns an id
lookup into an existence oracle. Nothing reads versions in bulk (content comes
one document at a time through `read_document`), so it stays as it is until
something does.

**The context deliberately does not contain the ACL.** A policy is checked
against the *querying* role, so every function a policy names has to be
executable by that role — and PostgREST exposes every executable function as an
RPC. Whatever `wiki.acl_context()` returns is therefore something any caller
can simply ask for. Returning the ACEs with their paths would hand them the
list of documents carrying a deny against them: the paths of the pages hidden
from them, without guessing one. That is the property this whole project turns
on.

So a source carries `sha256` of the path it sits on rather than the path, and
containment becomes equality against the hash of the target's prefix at the
same depth. A caller gets depths and flags — enough to confirm a path they
already guessed, which `wiki.can()` answers for any path they can name anyway,
and not enough to enumerate one they have not. Hiding the paths costs nothing
measurable, because the cheap tests run first: a source is dropped on its depth
and its inheritance flags before anything is hashed, and once a deeper source
has won, a shallower one is skipped without hashing at all.

A second implementation of a security rule is a thing to be nervous about.
`wiki.can()` stays as the specification, and `server/test/090_context_test.sql`
compares the two over the full cross product — every document, every
capability, every user, both values of `is_folder` — plus a subtree built with
one branch per ACE flag, and an assertion that each branch actually changed an
answer. It found one real bug: the loop returned NULL where `wiki.can()`
returns false, which a `WHERE` clause hides and a comparison does not.

Issue #10 has the profile and what is still open.

The cache stays regardless. It is correct, it is 60 lines, it costs nothing at
a hit, and it stops being invisible the moment syntax highlighting lands (#9),
which adds 8.57 ms per code block to the half of the page it does cover.

One thing the measurement changed: building the key cost 8.38 µs against a
0.62 µs lookup, because `config_digest` hashed the options dict on every
request. It is now memoised per backend instance — the lifetime over which the
answer cannot change — and the key costs 1.93 µs.

### Conformance, and why it is not optional

A plugin seam is a promise about behaviour, and a promise nobody checks is a
comment. `test/test_render.py` runs the same cases against **every
registered backend**: raw HTML never survives, no `javascript:` href survives,
a wikilink becomes an anchor under the reserved prefix, a forbidden link is
byte-identical to a missing one, and the renderer id names the engine.

It does not compare HTML byte for byte. Engines differ on whitespace, attribute
order and whether a lone image gets a paragraph, and none of that matters.

It earned its place on the first run. The neutral link form was originally a
custom URL scheme, `fswiki:`, which markdown-it-py was perfectly happy with —
and which **mistune rewrote to `#harmful-link`**, because it allowlists link
schemes for exactly the reason we do. The target was gone before the post-pass
could see it. A relative path under a reserved prefix, `/-/fswiki/<ltree>`, is
an ordinary link that every engine accepts, and `/-/` can never collide with a
document because a slug may be neither empty nor contain a slash.

That is the whole argument for building two backends rather than one: a seam
with a single implementation is not a seam, it is an abstraction that has never
met a second case and therefore still encodes the first one's assumptions.

## Locally

**One renderer, two callers.** A local preview that uses a different engine
from the server is a preview that lies, and it lies exactly where markdown
implementations disagree, which is where you most want to know. So the renderer
belongs in `fswiki-core`, next to `merge.py` and `naming.py`, for the same
reason those live there — shared by the CLI and the mount, dependent on
neither.

Local rendering has two requirements the server does not:

- **Drafts.** A draft has no revision, so it has no cache key, and it changes on
  every save. It is rendered every time. At 0.6 ms nobody notices.
- **Offline.** Reading through the mount works with no network; preview should
  too. Markdown to HTML is pure and needs nothing remote. Only link liveness
  needs the tree, and the mount is already holding it.

The shape:

    fswiki render engineering/onboarding.md     # HTML to stdout
    fswiki render --draft engineering/notes.md   # your unpublished version
    fswiki render --raw ...                      # links left as a cache holds them
    fswiki preview                               # localhost, watches, reloads

Both exist. `render` came first because it is the composable half and the
previewer's own inner loop, and because a thing that prints to stdout can be
tested by a shell script the way everything else here is. `--raw` prints
exactly what a shared cache would hold, which is the only way to see the
difference between the two halves of the split above.

`preview` is **read-only by construction**: every method but GET and HEAD is
refused before the request is routed, so the property survives whatever routes
get added later. It is not, however, *authenticated* — it holds one token and
answers as one identity, which is right for a preview and is why binding it to
a non-loopback address says out loud what that exposes. The remote service
below is the one that has to make a per-visitor permission decision, and it is
a different program for that reason.

`preview` is editor-agnostic on purpose. vim, emacs and VS Code all save
through the rename dance the mount already handles; a browser pointed at
localhost works for all three without a plugin for any of them.

## Remotely

A small service in front of PostgREST, not PostgREST itself — PostgREST returns
JSON and has no opinion about HTML, which is the correct amount of opinion for
it to have.

Per request it:

1. takes the caller's token and reads the document **over `POST
   /rpc/view_document`**;
2. renders, or takes the cached body for that `(document_id, version)`;
3. walks the neutral links against what this caller may read;
4. composes the shell — tree, breadcrumbs, affordances.

Step 1 is worth noticing: that is the audited read from
[audit-trail.md](audit-trail.md), so **page views are audited for free**, by
the same mechanism and with the same one round trip. A renderer that fetched
over GET would be a hole in the trail, and the reason the POST read exists is
that a GET cannot record itself.

It is `view_document` and not `read_document`, and the difference is the whole
of the `sync` capability. `read_document` reads through `syncable_document`, so
a page whose `sync` has been denied comes back as no rows — correct for a FUSE
mount, which is being told it may not take a copy, and enforced by the server
rather than by the client remembering to. But denying `sync` is supposed to
leave a page *readable in a browser* while keeping it off laptops, with every
view costing a request the server can log. A renderer reading through the sync
view could not serve those pages at all, so the lever would destroy the very
trail it exists to produce. `view_document` reads through `current_document`
and is gated on `read`.

Two functions rather than one with a flag, because which view a caller reads
through is a permission decision, and a permission decision that arrives as an
argument is one the caller gets to make. They are two grants instead.

## Deliberately not yet

- **Attachments and images.** An image referencing a wiki path needs the ACL
  applied to something that is not a document, which is a schema question
  before it is a rendering one.
- **Diagrams.** Publish-time materialisation, not the read path. Every diagram
  renderer worth having is a subprocess, and a subprocess in a read path is a
  sandbox, a timeout and a concurrency cap. Maths turned out not to need any of
  that; see above.
- **Search.** A different problem that happens to also read every document.
- **Themes.** Nothing about them is hard; nothing about them is interesting
  until there is a second reader.
