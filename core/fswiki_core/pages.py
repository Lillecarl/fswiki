"""The wiki as pages, for anything that puts it in front of a browser.

Two programs do that: `fswiki preview`, which shows you your own drafts on
your own machine, and the server, which shows published revisions to whoever
is asking. They are the same wiki and they should look and behave like it, so
everything down to the stylesheet lives here and the two of them differ in
what they are given rather than in what they do:

    Pages(client, drafts=True,  live_reload=True)    # preview
    Pages(client, drafts=False, live_reload=False)   # the server

What stays outside is the plumbing. Preview is a blocking http.server behind
an anyio portal because it is one person on a laptop; the server is ASGI
because it is not. Neither of those facts should be able to change the HTML.

The URL scheme is part of the sharing. `/-/` is reserved -- the same
reservation render.links makes -- and everything else is a display path, so a
link that works in one works in the other.
"""

from __future__ import annotations

import html
import re
import urllib.parse

from . import naming, render
from .client import Client, PostgrestError, Unreachable
from .render.frontmatter import Options

# Everything a server owns lives under this prefix, so it can never collide
# with a document path. The same reservation render.links makes.
RESERVED = "/-/"

HTML = "text/html; charset=utf-8"
TEXT = "text/plain; charset=utf-8"

STYLE = """
.acting{background:#7c2d12;color:#fff;padding:.4rem .8rem;font:600 .85rem/1.4
  system-ui,sans-serif;letter-spacing:.02em}
/* One token set, both schemes. The page is text and the styling should get
   out of its way; the only things that earn colour are links and the state
   line, because those are the two things you look for rather than read. */
:root {
  --bg: #fdfdfc; --fg: #22201d; --dim: #6b6862;
  --rule: #e3e0da; --tint: #f4f2ee; --link: #2c5f8a;
  --measure: 38rem;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181a; --fg: #d6d3cd; --dim: #8b8781;
    --rule: #2b2e31; --tint: #1e2124; --link: #7fb2dc;
  }
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--fg);
  max-width: var(--measure); margin: 0 auto; padding: 0 1.25rem 6rem;
  font: 17px/1.65 ui-serif, Charter, "Bitstream Charter", Georgia, serif;
  -webkit-text-size-adjust: 100%;
}
a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 2px; }

/* The one layout beyond the default. A document asks for it by name and this
   file decides what the name means -- the value never becomes a width, a
   class or a property. See render.frontmatter and issue #5. */
body.wide { --measure: 62rem; }

header {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 1rem; margin: 0 -1.25rem 2.5rem; padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--rule);
  font-family: ui-sans-serif, system-ui, sans-serif;
  position: sticky; top: 0; background: var(--bg);
}
header a.brand { color: var(--fg); text-decoration: none; font-weight: 600;
                 letter-spacing: .02em; }
.state { font-size: .8rem; color: var(--dim); text-align: right;
         font-variant-numeric: tabular-nums; }

h1, h2, h3, h4, h5, h6 {
  font-family: ui-sans-serif, system-ui, sans-serif;
  line-height: 1.25; margin: 2.2rem 0 .6rem; font-weight: 620;
}
h1 { font-size: 1.7rem; margin-top: 0; letter-spacing: -.01em; }
h2 { font-size: 1.3rem; }
h3 { font-size: 1.1rem; }
p, ul, ol, blockquote, table, pre { margin: 0 0 1.1rem; }

code, pre, kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                 font-size: .875em; }
code { background: var(--tint); padding: .12em .35em; border-radius: 3px; }
pre { background: var(--tint); border: 1px solid var(--rule); border-radius: 6px;
      padding: .85rem 1rem; overflow-x: auto; line-height: 1.5; }
pre code { background: none; padding: 0; }

blockquote { border-left: 2px solid var(--rule); margin-left: 0;
             padding-left: 1rem; color: var(--dim); }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2rem 0; }

table { border-collapse: collapse; width: 100%; font-size: .93em;
        font-family: ui-sans-serif, system-ui, sans-serif; display: block;
        overflow-x: auto; }
th, td { border-bottom: 1px solid var(--rule); padding: .45rem .7rem;
         text-align: left; }
th { font-weight: 600; }
tbody tr:last-child td { border-bottom: 0; }

/* Maths. Browsers lay MathML out themselves, so the only job here is to keep
   a wide expression from widening the page, and to make a failed conversion
   look like the source it is. See render.maths. */
.math.block { overflow-x: auto; margin: 0 0 1.1rem; }
tt.math { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: .875em; color: var(--dim); }

/* Syntax highlighting. pygments' short class names, which is also what
   docutils emits with syntax_highlight="short" -- so one set of rules covers
   markdown and reStructuredText both. See render.highlight.

   Trimmed on purpose, and measured: `get_style_defs()` for two schemes is
   8,853 B, against the 2,168 B here. This stylesheet was 3,579 B, so
   pygments' own would have made it three and a half times the size and this
   makes it one and a half. These are the classes twelve languages actually
   emitted, grouped by what they mean rather than by which lexer produced
   them, and anything unlisted keeps the colour of the surrounding text --
   which is the right way for this to be wrong. Scoped under `pre`, so a
   `class="k"` in prose is still prose. */
:root {
  --hl-comment: #7a7368; --hl-keyword: #8f4b26; --hl-string: #3f6b46;
  --hl-number: #7a4c8f; --hl-name: #1f5c7a; --hl-bad: #a02020;
}
@media (prefers-color-scheme: dark) {
  :root {
    --hl-comment: #8b8781; --hl-keyword: #d99a6c; --hl-string: #9ac48a;
    --hl-number: #c3a0e0; --hl-name: #7fb2dc; --hl-bad: #e88b8b;
  }
}
pre .c,pre .c1,pre .cm,pre .ch,pre .cs,pre .cp,pre .cpf
  { color: var(--hl-comment); font-style: italic; }
pre .k,pre .kc,pre .kd,pre .kn,pre .kp,pre .kr,pre .kt,pre .ow
  { color: var(--hl-keyword); }
pre .s,pre .s1,pre .s2,pre .sa,pre .sb,pre .sc,pre .sd,pre .se,pre .sh,
pre .si,pre .sr,pre .ss,pre .sx,pre .dl,pre .l
  { color: var(--hl-string); }
pre .m,pre .mb,pre .mf,pre .mh,pre .mi,pre .mo,pre .il
  { color: var(--hl-number); }
pre .nf,pre .nc,pre .nn,pre .nd,pre .ne,pre .fm,pre .nt,pre .na
  { color: var(--hl-name); }
pre .nb,pre .bp,pre .no,pre .nv,pre .vc,pre .vg,pre .vi
  { color: var(--hl-name); opacity: .85; }
/* A lexer says `err` where it lost track. Underlined rather than shouted:
   an author's half-written line is not an error the reader has to act on. */
pre .err { color: var(--hl-bad); text-decoration: underline wavy; }
/* Diffs are read as a whole, so these carry the line and not just a token. */
pre .gd { color: var(--hl-bad); }
pre .gi { color: var(--hl-string); }
pre .gh,pre .gu { color: var(--hl-comment); font-weight: 600; }
pre .ge { font-style: italic; }
pre .gs { font-weight: 600; }

/* Search. The box rides in the header on every page, so it has to be small
   enough not to push the breadcrumb around on a phone. */
form.find { margin: 0; }
form.find input {
  font: inherit; font-size: .85rem; font-family: ui-sans-serif, system-ui, sans-serif;
  background: var(--tint); color: var(--fg); border: 1px solid var(--rule);
  border-radius: 4px; padding: .2rem .5rem; width: 9rem; max-width: 30vw;
}
ol.results { list-style: none; padding: 0; margin: 0; }
ol.results li { margin: 0 0 1.6rem; }
ol.results .where { font-family: ui-sans-serif, system-ui, sans-serif;
                    font-size: .95rem; }
ol.results .excerpt { color: var(--dim); font-size: .95rem; margin: .25rem 0 0; }
/* The highlight the excerpt asked for. `mark` rather than a colour of its own:
   the default is a background, which survives both schemes. See render.search
   -- the marks arrive as control characters and become tags here, never
   before. */
mark { background: var(--tint); color: var(--fg); font-weight: 600;
       padding: 0 .1em; border-radius: 2px; }

/* The index. Indentation carries the tree, so nothing else has to. */
ul.tree { list-style: none; padding: 0; font-family: ui-sans-serif, system-ui, sans-serif;
          font-size: .95rem; }
ul.tree li { padding: .12rem 0; }
ul.tree a { text-decoration: none; }
ul.tree a:hover { text-decoration: underline; }
ul.tree .folder { color: var(--dim); font-size: .8rem; letter-spacing: .04em;
                  text-transform: uppercase; margin-top: 1rem; }
"""

#: What an attachment is served as, and what a browser may put in the page.
#:
#: Two rules from one set, and both matter:
#:
#: * A type in here is sent as itself with `Content-Disposition: inline`.
#: * Anything else is still sent as its declared type, but with
#:   `Content-Disposition: attachment`, which stops a browser from *rendering*
#:   it as a document when somebody navigates straight to it.
#:
#: That second rule is what makes an SVG safe without banning it. `<img>`
#: ignores `Content-Disposition`, so a diagram still draws in the page; a
#: direct visit downloads the file rather than opening a document that could
#: carry `<script>`. `attachment_headers` adds a CSP of its own on top, which
#: is the belt to that pair of braces -- and the raster types are in here
#: precisely because none of them can carry script at all.
INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}

#: Sent with every attachment, alongside the site-wide set.
#:
#: A second `content-security-policy` header rather than a different one: a
#: browser enforces every policy it is given, so two headers intersect and the
#: stricter wins. `sandbox` with no `allow-scripts` is what neutralises a file
#: opened as a document -- an SVG, an HTML file somebody uploaded -- even
#: before `Content-Disposition` gets a chance to.
ATTACHMENT_CSP = ("default-src 'none'; sandbox; base-uri 'none'; "
                  "form-action 'none'; frame-ancestors 'none'")


def attachment_headers(kind: str) -> list[tuple[str, str]]:
    """What a response of this kind needs beyond the site-wide headers.

    Keyed on the content type alone, so the preview and the server cannot
    disagree about it, and so a route added later gets the right headers by
    saying what it is serving rather than by remembering to ask.
    """
    if kind in (HTML, TEXT):
        return []
    disposition = "inline" if kind in INLINE else "attachment"
    # No `filename=`: an attachment is served at its own path, so the last
    # segment of the URL is already `logo.png` and the browser uses it. A
    # filename here would be a second copy of the same name, able to disagree.
    return [("content-disposition", disposition),
            ("content-security-policy", ATTACHMENT_CSP)]


#: The search box, on every page rather than on a page of its own. A wiki you
#: have to navigate to in order to search is a wiki people grep instead.
FIND = ("<form class=find action='" + RESERVED + "search' role=search>"
        "<input name=q type=search placeholder='Search' "
        "aria-label='Search this wiki'></form>")

RELOAD = """
(function () {
  var seen = null;
  setInterval(function () {
    fetch('/-/changed').then(function (r) { return r.text(); }).then(function (t) {
      if (seen === null) { seen = t; return; }
      if (t !== seen) { location.reload(); }
    }).catch(function () {});
  }, 2000);
})();
"""




#: What each layout does to the shell, written here rather than taken from the
#: document. A layout name that is missing from this map renders as the
#: default, so adding one to `frontmatter.LAYOUTS` without a rule for it is
#: harmless rather than an injection point.
LAYOUT = {"default": "", "wide": " class=wide"}


#: How `wiki.search` delimits a match inside an excerpt: STX and ETX.
#:
#: Control characters rather than tags, and that is the whole safety argument.
#: `ts_headline` does not escape what it is given, so its output is one
#: person's document with markers in it; markers that were already HTML would
#: arrive at another person's browser as markup they did not write. These two
#: cannot be mistaken for markup by anything, and `excerpt_html` below turns
#: them into tags only after everything around them has been escaped.
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"


def excerpt_html(text: str) -> str:
    """One search excerpt, escaped, with the matches marked.

    A small state machine rather than two `replace` calls, so the output is
    balanced whatever the input was. An author who types a STX into a page
    would otherwise open a `<mark>` that never closes and tint the rest of the
    results list; here the worst they achieve is a highlight in their own
    excerpt, which is what they asked for.
    """
    out: list[str] = []
    marking = False
    for chunk in re.split(f"([{MARK_OPEN}{MARK_CLOSE}])", text or ""):
        if chunk == MARK_OPEN:
            if not marking:
                out.append("<mark>")
                marking = True
        elif chunk == MARK_CLOSE:
            if marking:
                out.append("</mark>")
                marking = False
        else:
            out.append(html.escape(chunk))
    if marking:
        out.append("</mark>")
    return "".join(out)


def missing_body(path: str) -> str:
    """Why there is nothing here -- which must not say which reason.

    A page you may not read and a page that is not there are the same answer
    everywhere else in this project; a helpful error message is the easiest
    place to give that away.
    """
    return (f"<p>Nothing to show at <code>{html.escape(naming.to_display(path))}"
            f"</code>.</p><p>It may not exist, or it may not be yours to read \u2014 "
            f"this page is deliberately the same either way.</p>")


class Pages:
    """The read side of the wiki, assembled per request."""

    def __init__(self, client: Client, *, backend: str | None = None,
                 drafts: bool = False, banner: str | None = None,
                 live_reload: bool = False,
                 cache: render.cache.Cache | None = None) -> None:
        self._client = client
        self._backend = backend
        self._drafts = drafts
        # Shared between requests and owned by whatever built this, because a
        # cache that lives as long as one page is not a cache. None is a
        # working configuration, not a degraded one: `fswiki preview` renders
        # drafts, which have mutable content and no version, so there is no key
        # to give them.
        self._cache = cache
        # Set once by whatever constructed this, because the whole failure mode
        # of impersonation is forgetting you are doing it. A page that looks
        # exactly like your own wiki, minus a few things, is indistinguishable
        # from your own wiki having lost a few things -- so it goes on every
        # page rather than on a status screen someone has to think to visit.
        self._banner = banner
        self._live_reload = live_reload

    @property
    def url(self) -> str:
        """Where the wiki is, for saying so when it is not answering."""
        return str(self._client.base_url)

    # -- the shell ---------------------------------------------------------

    def shell(self, title: str, body: str, path: str, state: str | None,
              options: Options | None = None) -> str:
        """The page around the body, including what the document asked for.

        `options` is the allowlisted frontmatter, and it reaches exactly one
        thing: which of `LAYOUT` this page uses. It cannot reach the banner.
        That is deliberate and it is tested: the whole failure mode of
        impersonation is forgetting you are doing it, so a page that could
        suppress "viewing as X" could make another person's wiki look like
        your own -- and pages are written by whoever may write them, not by
        the person reading. See render.frontmatter.
        """
        crumb = html.escape(naming.to_display(path)) if path else "Contents"
        # The banner is composed from `self._banner`, which came from whatever
        # built this object, and from nothing in `options`.
        banner = (f"<div class=acting>viewing as {html.escape(self._banner)}"
                  f" &middot; read-only</div>" if self._banner else "")
        reload_script = f"<script>{RELOAD}</script>" if self._live_reload else ""
        layout = LAYOUT.get((options or Options()).layout, "")
        return (
            "<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body{layout}>"
            f"{banner}"
            f"<header><a class=brand href='/'>fswiki</a>"
            f"<span class=state>{crumb}{' &middot; ' + html.escape(state) if state else ''}"
            f"</span>{FIND}</header>{body}"
            f"{reload_script}</body></html>"
        )

    def unreachable(self) -> str:
        """The page for when the wiki is not answering.

        HTML rather than an exception name, because under live reload the tab
        comes back by itself when the wiki does, with nobody watching for the
        moment to press refresh.
        """
        return self.shell(
            "Cannot reach the wiki",
            f"<p>Nothing is answering at <code>{html.escape(self.url)}</code>.</p>"
            + ("<p>This page reloads itself when it comes back.</p>"
               if self._live_reload else ""),
            "", None)

    # -- reads -------------------------------------------------------------

    async def outline(self) -> list[dict]:
        """The tree: path and kind, for the index and for resolving links.

        Deliberately not manifest(). Nothing on a rendered page needs the
        capabilities column, and fetching it costs an ACL walk per capability
        per document -- 224 ms against 38 ms on the dev fixtures, which is most
        of what a page used to cost.
        """
        return await self._client.outline()

    async def visible(self) -> set[str]:
        return {row["path"] for row in await self.outline()}

    async def change_token(self) -> str:
        """The change token, for the reload poll. Eleven bytes."""
        try:
            return await self._client.change_token() or ""
        except (PostgrestError, Unreachable):
            # The poll is a convenience; a server that blinks should cost a
            # reload that does not happen, never a page that stops being served.
            return ""

    async def page(self, path: str) -> tuple[int, str, bytes]:
        """What is at this path: a rendered document, an attachment, or a 404.

        Three kinds now, so the answer carries its own content type rather
        than being assumed to be HTML. `attachment_headers` turns that type
        into the rest of the response.

        A document that is not there and one that is not ours to read produce
        the same answer, because the view behind this already refuses to tell
        them apart -- and telling them apart is how a link graph leaks.
        """
        draft = None
        if self._drafts:
            draft = next((d for d in await self._client.drafts()
                          if d["path"] == path and d.get("content") is not None),
                         None)

        if draft is not None:
            text = draft["content"]
            content_type = draft.get("content_type") or "text/markdown"
            state = "draft"
        else:
            row = await self._client.document(path)
            if row is None:
                return self._html(404, self.shell("Not here", missing_body(path),
                                                  path, None))
            if row.get("is_attachment"):
                return await self._file(path)
            text = row.get("content") or ""
            content_type = row.get("content_type") or "text/markdown"
            state = f"revision {row.get('version')}"

        # Split here rather than inside `_body`, because `_body` can skip the
        # render entirely on a cache hit and the cache stores HTML. The
        # document's own text is in hand either way, so this costs one string
        # comparison on the hot path. See render.frontmatter.
        options, source = render.frontmatter.split(text, content_type)

        try:
            neutral = self._body(source, content_type,
                                 None if draft is not None else row)
        except (render.UnknownBackend, render.safety.SanitiserUnavailable) as exc:
            return self._html(500, self.shell(
                "Cannot render", f"<p>{html.escape(str(exc))}</p>", path, None))

        visible = await self.visible()
        body = render.links.resolve(
            neutral,
            lambda target: "/" + naming.to_display(target) if target in visible else None,
        )
        return self._html(200, self.shell(naming.to_display(path), body, path,
                                          state, options))

    @staticmethod
    def _html(status: int, markup: str) -> tuple[int, str, bytes]:
        return status, HTML, markup.encode()

    async def _file(self, path: str) -> tuple[int, str, bytes]:
        """An attachment's bytes, or the same 404 anything else would give.

        A second request, because `current_document` deliberately does not
        join to `wiki.attachment`: that table's policy is an ACL walk per row,
        measured at 0.35 ms, and the view is read by every request -- 138 ms
        added to a 1,420-row manifest for 400 files. One extra round trip on
        an image is the cheaper half of that trade.

        The type is narrowed here rather than trusted. The database already
        refuses a media type that could carry a header, and `INLINE` decides
        what a browser is allowed to do with it.
        """
        found = await self._client.attachment(path)
        if found is None:
            # It was there a request ago. Same answer as never having been,
            # because that is the answer every other read gives.
            return self._html(404, self.shell("Not here", missing_body(path),
                                              path, None))
        return 200, found.get("media_type") or "application/octet-stream", \
            found.get("bytes") or b""

    def _body(self, text: str, content_type: str, row: dict | None) -> str:
        """The rendered body with its links still neutral, cached where it can be.

        `row` is the published document, or None for a draft. A draft is never
        cached: its content is mutable and it has no version, so the key that
        makes this safe does not exist for it.

        What is stored is this -- the neutral body -- and not the composed page.
        Link resolution runs per reader afterwards, and caching after it would
        serve one reader's link graph to another.
        """
        key = None
        if self._cache is not None and row is not None and row.get("version") is not None:
            key = render.cache.Key(row["id"], row["version"],
                                   render.renderer_id(content_type, self._backend))
            stored = self._cache.get(key)
            if stored is not None:
                return stored

        rendered = render.render(text, content_type=content_type,
                                 backend=self._backend)
        if key is not None:
            self._cache.put(key, rendered.html)
        return rendered.html

    # -- search ------------------------------------------------------------

    async def search(self, query: str, *, limit: int = 20) -> list[dict]:
        """Ranked matches for `query`, drafts folded in where this is a preview.

        The filtering is not done here and could not be: `wiki.search` applies
        the caller's own `read`, so a page this reader may not see never
        reaches this process. That is the same reason `page()` does not check
        anything either.

        A draft of a page that is also published wins, because a draft is what
        its author sees everywhere else -- in the mount, in `fswiki status` and
        on the page itself. Search disagreeing would be search being wrong.
        """
        rows = [dict(row, state=None) for row in
                await self._client.search(query, limit=limit)]
        if self._drafts:
            drafts = [dict(row, state="draft") for row in
                      await self._client.search(query, limit=limit, drafts=True)]
            drafted = {row["path"] for row in drafts}
            rows = drafts + [row for row in rows if row["path"] not in drafted]
        rows.sort(key=lambda row: (-(row.get("rank") or 0.0), row["path"]))
        return rows[:limit]

    async def search_page(self, query: str) -> str:
        """The results, or the reason there are none -- which is always the same
        reason.

        "Nothing matches" has to be the answer for a word that is in no page
        and for a word that is only in a page this reader may not open. Saying
        "3 results you cannot see" would turn the search box into an oracle
        over the whole wiki, which is the thing `missing_body` is careful about
        one route along.
        """
        terms = (query or "").strip()
        box = ("<form class=find action='" + RESERVED + "search' role=search>"
               f"<input name=q type=search autofocus value=\"{html.escape(terms)}\" "
               "aria-label='Search this wiki'></form>")
        if not terms:
            return self.shell("Search", f"<h1>Search</h1>{box}", "", None)

        rows = await self.search(terms)
        if not rows:
            body = (f"<h1>Search</h1>{box}"
                    "<p>Nothing here matches that.</p>")
            return self.shell("Search", body, "", None)

        items = []
        for row in rows:
            display = naming.to_display(row["path"])
            state = (f" <span class=state>{html.escape(row['state'])}</span>"
                     if row.get("state") else "")
            items.append(
                f'<li><div class=where><a href="/{html.escape(display)}">'
                f"{html.escape(display)}</a>{state}</div>"
                f"<p class=excerpt>{excerpt_html(row.get('excerpt') or '')}</p></li>")
        body = (f"<h1>Search</h1>{box}"
                f"<ol class=results>{''.join(items)}</ol>")
        return self.shell("Search", body, "", None)

    async def index(self) -> str:
        rows = sorted(await self.outline(), key=lambda r: r["path"])
        items = []
        for row in rows:
            display = naming.to_display(row["path"])
            if not display or display == "/":
                continue
            depth = display.count("/")
            if row.get("is_folder"):
                items.append(
                    f'<li style="margin-left:{depth}em" class="folder">'
                    f'{html.escape(display)}/</li>')
            else:
                items.append(
                    f'<li style="margin-left:{depth}em">'
                    f'<a href="/{html.escape(display)}">{html.escape(display)}</a></li>')
        return self.shell("Contents", f"<ul class=tree>{''.join(items)}</ul>", "", None)

    # -- routing -----------------------------------------------------------

    async def respond(self, route: str, query: str = "") -> tuple[int, str, bytes]:
        """One GET, as (status, content type, body).

        `query` is the raw query string, unparsed, because exactly one route
        wants it and handing the rest of them a parsed dict would invite the
        others to grow options nobody asked for.

        Shared so that a link that works in the preview works in the server.
        Note what it does not take: a method. Deciding that only GET and HEAD
        exist belongs to the thing holding the socket, and it belongs *before*
        routing, so that read-only is a property of the server rather than a
        claim about which routes happen to exist today.
        """
        if route == RESERVED + "search":
            terms = urllib.parse.parse_qs(query).get("q", [""])[0]
            return 200, HTML, (await self.search_page(terms)).encode()

        if route == RESERVED + "changed":
            if not self._live_reload:
                return 404, TEXT, b"no such thing\n"
            return 200, TEXT, (await self.change_token()).encode()

        if route.startswith(RESERVED + "fswiki/"):
            # A link render.links could not resolve. Rather than serving it,
            # answer the permission question it represents.
            target = route[len(RESERVED + "fswiki/"):]
            if target in await self.visible():
                return 200, HTML, self.shell(
                    "Redirecting", f'<meta http-equiv=refresh '
                    f'content="0;url=/{html.escape(naming.to_display(target))}">',
                    target, None).encode()
            return 404, HTML, self.shell(
                "Not here", missing_body(target), target, None).encode()

        if route.startswith(RESERVED):
            return 404, TEXT, b"no such thing\n"

        if route in ("/", ""):
            return 200, HTML, (await self.index()).encode()

        wanted = route.lstrip("/")
        try:
            # from_route, not from_display: a URL ending in `.png` is somebody
            # asking for a file, where a wikilink ending in `.tar.gz` should
            # stay literal text. See naming.from_route.
            path = wanted if naming.looks_like_ltree(wanted) else naming.from_route(wanted)
        except ValueError:
            return 404, HTML, self.shell(
                "Not here", "<p>Not a path this wiki can hold.</p>", "", None).encode()

        return await self.page(path)
