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
| **a cache hit** | **0.17 µs** | a dict lookup |

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
      markdown-it-py   4.2.0      text/markdown
      mistune          3.3.3      text/markdown
      plain            1          text/plain

`markdown-it-py` is preferred where present, because CommonMark is a
specification rather than a dialect and the filesystem is the source of truth —
files here get edited by tools that know nothing about us, and a document should
mean the same thing to all of them. There is also a markdown-it in JavaScript,
which is the only way a future browser-side live preview agrees with the server
without shipping two dialects. `mistune` is within 2 % on speed and ships as the
second opinion. `plain` needs no library at all.

### The renderer is part of the cache key

`Rendered.renderer` identifies the whole pipeline, not just the engine:

    markdown-it-py/4.2.0+fswiki1

That goes in the cache key beside `(document_id, version)`. Leave it out and
switching engines — or upgrading one — quietly serves output that the code now
running would not produce. The `+fswiki<n>` suffix is the pre- and post-passes'
own version, because they affect the bytes too.

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
- **Diagrams and maths.** Publish-time materialisation by the rule above, not
  the read path.
- **Search.** A different problem that happens to also read every document.
- **Themes.** Nothing about them is hard; nothing about them is interesting
  until there is a second reader.
