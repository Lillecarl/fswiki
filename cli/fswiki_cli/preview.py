"""A local web view of the wiki, for looking at what you are writing.

`fswiki render` prints one document; this is the same pipeline with a shell
around it and a URL per page, which is what makes it usable while editing.

**It is read-only by construction, not by convention.** The handler refuses
every method except GET and HEAD before it looks at the path, so "read-only"
does not depend on which routes happen to exist today or on nobody adding a
form later. Nothing in this module calls a write method on the client.

That is a different claim from "safe to expose", and the difference matters
when `--host` is not loopback. The server holds *your* token and answers as
you, so anyone who can reach the port reads everything you can read. There is
no login, and no per-visitor permission check to make, because there is only
ever one identity involved. Binding it to the world publishes your view of the
wiki to whoever finds the port; the code says so at startup rather than
assuming you meant it.

Threading: `http.server` is blocking and the client is async, so the HTTP
server runs in a worker thread and reaches the event loop through an anyio
portal. One client, one connection pool, no second HTTP stack.
"""

from __future__ import annotations

import html
import http.server
import socket
import sys
import threading
import urllib.parse

import anyio
import anyio.from_thread

from fswiki_core import render
from fswiki_core.client import Client, PostgrestError

from . import paths

# Everything the server owns lives under this prefix, so it can never collide
# with a document path. It is the same reservation render.links makes.
RESERVED = "/-/"


class Preview:
    """The read side of the wiki, assembled per request."""

    def __init__(self, client: Client, *, backend: str | None = None,
                 drafts: bool = True) -> None:
        self._client = client
        self._backend = backend
        self._drafts = drafts

    async def manifest(self) -> list[dict]:
        return await self._client.manifest()

    async def visible(self) -> set[str]:
        return {row["path"] for row in await self.manifest()}

    async def page(self, path: str) -> tuple[int, str]:
        """One rendered document, or a not-found page.

        A document that is not there and one that is not ours to read produce
        the same answer, because `syncable_document` already refuses to tell
        them apart — and telling them apart is how a link graph leaks.
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
                return 404, _shell("Not here", _missing(path), path, None)
            text = row.get("content") or ""
            content_type = row.get("content_type") or "text/markdown"
            state = f"revision {row.get('version')}"

        try:
            rendered = render.render(text, content_type=content_type,
                                     backend=self._backend)
        except (render.UnknownBackend, render.safety.SanitiserUnavailable) as exc:
            return 500, _shell("Cannot render", f"<p>{html.escape(str(exc))}</p>",
                               path, None)

        visible = await self.visible()
        body = render.links.resolve(
            rendered.html,
            lambda target: "/" + paths.to_display(target) if target in visible else None,
        )
        return 200, _shell(paths.to_display(path), body, path, state)

    async def index(self) -> str:
        rows = sorted(await self.manifest(), key=lambda r: r["path"])
        items = []
        for row in rows:
            display = paths.to_display(row["path"])
            if not display:
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
        return _shell("Contents", f"<ul class=tree>{''.join(items)}</ul>", "", None)

    async def token(self) -> str:
        """The change token, for the reload poll. Eleven bytes."""
        try:
            return await self._client.change_token() or ""
        except PostgrestError:
            return ""


def _missing(path: str) -> str:
    return (f"<p>Nothing to show at <code>{html.escape(paths.to_display(path))}"
            f"</code>.</p><p>It may not exist, or it may not be yours to read — "
            f"this page is deliberately the same either way.</p>")


_STYLE = """
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

# Reload on change, by polling the same eleven-byte token the mount polls.
# Deliberately a poll and not a socket: there is no write path here to keep
# open, and this way the whole server stays GET-only.
_RELOAD = """
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


def _shell(title: str, body: str, path: str, state: str | None) -> str:
    crumb = html.escape(paths.to_display(path)) if path else "Contents"
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>"
        f"<header><a class=brand href='/'>fswiki</a>"
        f"<span class=state>{crumb}{' &middot; ' + html.escape(state) if state else ''}"
        f"</span></header>{body}"
        f"<script>{_RELOAD}</script></body></html>"
    )


def _handler(preview: Preview, portal: anyio.from_thread.BlockingPortal):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "fswiki-preview"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

        # Every method other than these two is refused here, before any routing
        # happens. That is what makes "read-only" a property of the server
        # rather than a claim about its routes.
        def do_POST(self): self._refuse()
        def do_PUT(self): self._refuse()
        def do_PATCH(self): self._refuse()
        def do_DELETE(self): self._refuse()

        def _refuse(self):
            self._send(405, "text/plain; charset=utf-8",
                       b"this server only reads\n", allow="GET, HEAD")

        def do_HEAD(self):
            self.do_GET(body=False)

        def do_GET(self, body: bool = True):
            route = urllib.parse.urlsplit(self.path).path
            try:
                status, kind, payload = portal.call(_respond, preview, route)
            except Exception as exc:  # noqa: BLE001 - one bad page, not a dead server
                status, kind = 500, "text/plain; charset=utf-8"
                payload = f"{type(exc).__name__}: {exc}\n".encode()
            self._send(status, kind, payload, body=body)

        def _send(self, status: int, kind: str, payload: bytes, *,
                  body: bool = True, allow: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            # A preview is only useful if it is not cached.
            self.send_header("Cache-Control", "no-store")
            if allow:
                self.send_header("Allow", allow)
            self.end_headers()
            if body:
                self.wfile.write(payload)

    return Handler


async def _respond(preview: Preview, route: str) -> tuple[int, str, bytes]:
    html_type = "text/html; charset=utf-8"

    if route == "/-/changed":
        return 200, "text/plain; charset=utf-8", (await preview.token()).encode()

    if route.startswith(RESERVED + "fswiki/"):
        # A link that was never resolved. Rather than serving it, answer the
        # permission question it represents and redirect or refuse.
        target = route[len(RESERVED + "fswiki/"):]
        if target in await preview.visible():
            return 200, html_type, _shell(
                "Redirecting", f'<meta http-equiv=refresh '
                f'content="0;url=/{html.escape(paths.to_display(target))}">',
                target, None).encode()
        return 404, html_type, _shell("Not here", _missing(target), target, None).encode()

    if route.startswith(RESERVED):
        return 404, "text/plain; charset=utf-8", b"no such thing\n"

    if route in ("/", ""):
        return 200, html_type, (await preview.index()).encode()

    try:
        path = paths.resolve(urllib.parse.unquote(route.lstrip("/")))
    except paths.PathError:
        return 404, html_type, _shell("Not here", "<p>Not a path this wiki can hold.</p>",
                                      "", None).encode()

    status, page = await preview.page(path)
    return status, html_type, page.encode()


async def serve(client: Client, *, host: str, port: int,
                backend: str | None = None, drafts: bool = True) -> int:
    """Run until interrupted. Returns a process exit code."""
    preview = Preview(client, backend=backend, drafts=drafts)

    # Fail before binding if the renderer is not installed, rather than
    # serving a wall of 500s.
    try:
        render.render("# ok", backend=backend)
    except (render.UnknownBackend, render.safety.SanitiserUnavailable) as exc:
        print(f"fswiki: {exc}", file=sys.stderr)
        return 1

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    async with anyio.from_thread.BlockingPortal() as portal:
        httpd = Server((host, port), _handler(preview, portal))
        shown = host if host not in ("0.0.0.0", "::") else _guess_address()
        print(f"fswiki preview on http://{shown}:{httpd.server_address[1]}/",
              file=sys.stderr)
        if host not in ("127.0.0.1", "::1", "localhost"):
            # Said plainly, once. The exposure is not that the server writes —
            # it cannot — but that it reads as you, for anyone who connects.
            print(f"  listening on {host}: anyone who can reach this port reads "
                  f"everything your token can read, with no login.",
                  file=sys.stderr)
        print("  read-only; ctrl-c to stop", file=sys.stderr)

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            await anyio.sleep_forever()
        finally:
            httpd.shutdown()
            httpd.server_close()
    return 0


def _guess_address() -> str:
    """A hostname worth printing when bound to every interface."""
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return "localhost"
