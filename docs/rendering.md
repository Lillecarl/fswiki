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
| one code block through pygments | 1.81 ms | |
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

### The renderer is part of the cache key

`Rendered.renderer` identifies the whole pipeline, not just the engine:

    markdown-it-py/4.2.0+cfgfff05eca+fswiki4

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
it covers — and **that step is six per cent of the page**. Two PostgREST round
trips are 167 ms of the 168. Switching the cache off and on again moves the
end-to-end number by less than the noise.

That is worth writing down rather than hiding, because it says what to do next:
**the render was never the problem.** The next real win is the two round trips —
`outline()` alone costs more than `document()`, and every page pays both. A
third of a second per page is not a rendering question.

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
