"""The HTTP surface: one ASGI app, no framework.

There is very little here, and that is the point. The pages come from
`fswiki_core.pages`, shared with `fswiki preview` so the two show the same
wiki; the identity comes from the visitor's own token, passed through to
PostgREST unexamined; and the permissions come from Postgres. What is left is
a socket, six response headers and a rule about which methods exist.

**Read-only by construction.** Every method except GET and HEAD is refused
before the path is looked at, exactly as in the preview -- so it does not
depend on which routes happen to exist today, or on nobody adding a form later.

**One identity per request, one pool for all of them.** The token changes from
one visitor to the next, so a Client is built per request; they share a
ClientPool's connections, because a fresh TCP connection to PostgREST per page
view would be a handshake per page. See fswiki_core.client.ClientPool.

**The read tree, not the sync tree.** Denying `sync` is documented as leaving a
page readable in a browser while keeping it off laptops, so this is the browser
that must still be able to read it. tree="read" reads through current_document
and the audited view_document(); a mirror's tree=\"sync\" is a different grant.
"""

from __future__ import annotations

import http.cookies
import logging

from fswiki_core import pages as pages_mod
from fswiki_core.client import ClientPool, PostgrestError, Unreachable
from fswiki_core.pages import Pages
from fswiki_core.render import cache as render_cache

from .config import Config

log = logging.getLogger(__name__)

# The cookie a session will land in once there is a login. Read already, and
# ahead of the header, so that the shape is settled before OIDC arrives and a
# test can drive either.
SESSION_COOKIE = "fswiki_session"

# Sent on everything.
#
# no-store rather than a validator, and it is not a performance oversight: the
# whole claim of wiki.view_document() is that the request which serves a page
# records the view, in the same transaction. A 304 is a view. Letting a browser
# answer one from its own cache is how the trail develops holes that depend on
# what somebody read earlier.
#
# The CSP is strict because it can be: the shell is one inline stylesheet and,
# in this program, no script at all -- live_reload is the preview's, not ours.
# A wiki renders text one person wrote for another to read, so the sanitiser in
# fswiki_core.render.safety is the first line and this is the second.
SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"content-security-policy",
     b"default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
     b"base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    # A wiki path is not a secret but it is not somebody else's business
    # either, and every external link in a rendered page would otherwise carry
    # the page it was on.
    (b"referrer-policy", b"no-referrer"),
]


def token_from(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The visitor's token, from a cookie or an Authorization header.

    Not verified here, and deliberately: PostgREST checks the signature and
    Postgres reads the claims. A server that decided for itself who a token
    belonged to would be a second place identity is established, and the one
    place it is established now would stop being the only one.

    Cookie first, because that is what a browser will carry once there is a
    login; the header is what a script or a test uses, and there is no reason
    to make either of them the odd one out.
    """
    raw: dict[bytes, bytes] = {}
    cookie_header = b""
    for name, value in headers:
        lowered = name.lower()
        if lowered == b"cookie":
            cookie_header = cookie_header + b"; " + value if cookie_header else value
        else:
            raw[lowered] = value

    if cookie_header:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(cookie_header.decode("latin-1"))
        except http.cookies.CookieError:
            jar = http.cookies.SimpleCookie()
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None and morsel.value:
            return morsel.value

    authorization = raw.get(b"authorization", b"").decode("latin-1")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


class Application:
    """The wiki over HTTP. One instance per process."""

    def __init__(self, config: Config, *, backend: str | None = None) -> None:
        self._config = config
        self._backend = backend
        self._pool = ClientPool()
        # One per process and shared by every request, which is the only way a
        # cache of rendered bodies is worth anything. Nothing in it is ever
        # invalidated: see fswiki_core.render.cache.
        self.cache = (render_cache.Cache(config.render_cache_bytes)
                      if config.render_cache_bytes > 0 else None)

    async def aclose(self) -> None:
        await self._pool.aclose()

    def pages_for(self, token: str | None) -> tuple[Pages, object]:
        """A view of the wiki as whoever is asking.

        drafts=False: a draft belongs to the person who wrote it and is not
        published. This program shows published revisions; the preview is where
        you look at your own unpublished work.
        """
        client = self._pool.client(
            self._config.postgrest_url, token, tree="read")
        return Pages(client, backend=self._backend, drafts=False,
                     cache=self.cache), client

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":  # pragma: no cover - uvicorn sends no others
            return

        method = scope["method"]
        if method not in ("GET", "HEAD"):
            await self._send(send, 405, pages_mod.TEXT, b"this server only reads\n",
                             extra=[(b"allow", b"GET, HEAD")])
            return

        status, kind, body = await self._respond(scope)
        await self._send(send, status, kind, body, body_bytes=method != "HEAD")

    async def _respond(self, scope: dict) -> tuple[int, str, bytes]:
        pages, client = self.pages_for(token_from(scope.get("headers", [])))
        try:
            return await pages.respond(
                scope["path"], scope.get("query_string", b"").decode("latin-1"))
        except Unreachable:
            # 502 rather than 500: this server is fine, the one behind it is
            # not, and saying so is the difference between "we are broken" and
            # "the database is".
            return 502, pages_mod.HTML, pages.unreachable().encode()
        except PostgrestError as exc:
            if exc.status in (401, 403):
                # A token PostgREST will not take. Not a permission decision
                # about a document -- those come back as an empty result and
                # are already a 404 -- but a decision about the token itself.
                return exc.status, pages_mod.HTML, pages.shell(
                    "Not signed in",
                    "<p>This wiki did not accept that token.</p>",
                    "", None).encode()
            log.exception("postgrest refused a read")
            return 502, pages_mod.HTML, pages.unreachable().encode()
        finally:
            # Returns the client, not the connections: aclose() on a pooled
            # client is a no-op against the shared transport.
            await client.aclose()

    @staticmethod
    async def _send(send, status: int, kind: str, payload: bytes, *,
                    body_bytes: bool = True,
                    extra: list[tuple[bytes, bytes]] | None = None) -> None:
        headers = [
            (b"content-type", kind.encode()),
            (b"content-length", str(len(payload)).encode()),
            *SECURITY_HEADERS,
            *(extra or []),
        ]
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        await send({"type": "http.response.body",
                    "body": payload if body_bytes else b""})

    async def _lifespan(self, receive, send) -> None:
        """Answer uvicorn's startup and shutdown, and close the pool on the way
        out. Without this uvicorn logs that lifespan is unsupported and the
        connections leak until the process dies."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self.cache is not None:
                    log.info("render cache: %s", self.cache.stats())
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return
