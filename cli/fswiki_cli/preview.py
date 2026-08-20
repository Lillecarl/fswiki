"""A local web view of the wiki, for looking at what you are writing.

The pages themselves are `fswiki_core.pages`, shared with the server so that
the preview and the wiki people actually read look and behave like the same
thing. What is here is the plumbing that is *not* shared: a blocking
http.server behind an anyio portal, which is right for one person on a laptop
and wrong for anything else.

**It is read-only by construction, not by convention.** The handler refuses
every method except GET and HEAD before it looks at the path, so "read-only"
does not depend on which routes happen to exist today or on nobody adding a
form later. Nothing in this module or in `pages` calls a write method.

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

from fswiki_core import pages as pages_mod
from fswiki_core import render
from fswiki_core.client import Client, Unreachable
from fswiki_core.pages import Pages


def _handler(pages: Pages, portal: anyio.from_thread.BlockingPortal):
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
            split = urllib.parse.urlsplit(self.path)
            try:
                status, kind, payload = portal.call(
                    pages.respond, split.path, split.query)
            except Unreachable:
                # 502 rather than 500: this server is fine, the one behind it is
                # not, and a preview left open in a tab is going to meet that
                # every time a laptop sleeps. An HTML page rather than the
                # exception name below, because it carries the reload poll --
                # so the tab comes back by itself when the wiki does, with
                # nobody watching for the moment to press refresh.
                status, kind = 502, pages_mod.HTML
                payload = pages.unreachable().encode()
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


async def serve(client: Client, *, host: str, port: int,
                backend: str | None = None, drafts: bool = True,
                acting_as: str | None = None) -> int:
    """Run until interrupted. Returns a process exit code."""
    pages = Pages(client, backend=backend, drafts=drafts, banner=acting_as,
                  # A preview is for while you are writing, so it reloads
                  # itself. The server serves many people and has no drafts of
                  # theirs to notice changing; it does not.
                  live_reload=True)

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
        httpd = Server((host, port), _handler(pages, portal))
        shown = host if host not in ("0.0.0.0", "::") else _guess_address()
        print(f"fswiki preview on http://{shown}:{httpd.server_address[1]}/",
              file=sys.stderr)
        if host not in ("127.0.0.1", "::1", "localhost"):
            # Said plainly, once. The exposure is not that the server writes —
            # it cannot — but that it reads as you, for anyone who connects.
            print(f"  listening on {host}: anyone who can reach this port reads "
                  f"everything your token can read, with no login.",
                  file=sys.stderr)
        if acting_as:
            print(f"  showing the view of {acting_as} — not yours, and the "
                  f"server has a record of it", file=sys.stderr)
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
