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

from . import naming, render
from .client import Client, PostgrestError, Unreachable

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

/* The index. Indentation carries the tree, so nothing else has to. */
ul.tree { list-style: none; padding: 0; font-family: ui-sans-serif, system-ui, sans-serif;
          font-size: .95rem; }
ul.tree li { padding: .12rem 0; }
ul.tree a { text-decoration: none; }
ul.tree a:hover { text-decoration: underline; }
ul.tree .folder { color: var(--dim); font-size: .8rem; letter-spacing: .04em;
                  text-transform: uppercase; margin-top: 1rem; }
"""

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
                 live_reload: bool = False) -> None:
        self._client = client
        self._backend = backend
        self._drafts = drafts
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

    def shell(self, title: str, body: str, path: str, state: str | None) -> str:
        crumb = html.escape(naming.to_display(path)) if path else "Contents"
        banner = (f"<div class=acting>viewing as {html.escape(self._banner)}"
                  f" &middot; read-only</div>" if self._banner else "")
        reload_script = f"<script>{RELOAD}</script>" if self._live_reload else ""
        return (
            "<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
            f"{banner}"
            f"<header><a class=brand href='/'>fswiki</a>"
            f"<span class=state>{crumb}{' &middot; ' + html.escape(state) if state else ''}"
            f"</span></header>{body}"
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

    async def manifest(self) -> list[dict]:
        return await self._client.manifest()

    async def visible(self) -> set[str]:
        return {row["path"] for row in await self.manifest()}

    async def change_token(self) -> str:
        """The change token, for the reload poll. Eleven bytes."""
        try:
            return await self._client.change_token() or ""
        except (PostgrestError, Unreachable):
            # The poll is a convenience; a server that blinks should cost a
            # reload that does not happen, never a page that stops being served.
            return ""

    async def page(self, path: str) -> tuple[int, str]:
        """One rendered document, or a not-found page.

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
                return 404, self.shell("Not here", missing_body(path), path, None)
            text = row.get("content") or ""
            content_type = row.get("content_type") or "text/markdown"
            state = f"revision {row.get('version')}"

        try:
            rendered = render.render(text, content_type=content_type,
                                     backend=self._backend)
        except (render.UnknownBackend, render.safety.SanitiserUnavailable) as exc:
            return 500, self.shell("Cannot render", f"<p>{html.escape(str(exc))}</p>",
                                   path, None)

        visible = await self.visible()
        body = render.links.resolve(
            rendered.html,
            lambda target: "/" + naming.to_display(target) if target in visible else None,
        )
        return 200, self.shell(naming.to_display(path), body, path, state)

    async def index(self) -> str:
        rows = sorted(await self.manifest(), key=lambda r: r["path"])
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

    async def respond(self, route: str) -> tuple[int, str, bytes]:
        """One GET, as (status, content type, body).

        Shared so that a link that works in the preview works in the server.
        Note what it does not take: a method. Deciding that only GET and HEAD
        exist belongs to the thing holding the socket, and it belongs *before*
        routing, so that read-only is a property of the server rather than a
        claim about which routes happen to exist today.
        """
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
            path = wanted if naming.looks_like_ltree(wanted) else naming.from_display(wanted)
        except ValueError:
            return 404, HTML, self.shell(
                "Not here", "<p>Not a path this wiki can hold.</p>", "", None).encode()

        status, page = await self.page(path)
        return status, HTML, page.encode()
