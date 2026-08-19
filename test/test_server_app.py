"""The HTTP surface, driven in-process.

httpx speaks ASGI directly, so these exercise the real application object --
routing, headers, token handling, the lot -- without binding a port or
starting uvicorn. What they do not cover is uvicorn itself, which is somebody
else's tested code.

The wiki behind them is the session's PostgREST. The app is pointed at it the
way a deployment would be: a host and a port, not an injected client.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from conftest import ROOT
from fswiki_server.app import SESSION_COOKIE, Application, token_from
from fswiki_server.config import Config

pytestmark = pytest.mark.anyio

PUBLIC = "root.notices"


@pytest.fixture
def config(stack):
    parsed = urllib.parse.urlsplit(stack.url)
    return Config(
        database_url=f"postgres://postgres@127.0.0.1:{stack.pg_port}/fswiki",
        schema_dir=ROOT / "server" / "schema",
        postgrest_host=parsed.hostname,
        postgrest_port=parsed.port,
    )


@pytest.fixture
async def browser(config):
    """An HTTP client for the app itself."""
    app = Application(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://wiki.test") as c:
        yield c
    await app.aclose()


@pytest.fixture
def published(stack):
    """A page granted to `public`, removed afterwards. As in test_public.py:
    not a fixture in 010_fixtures.sql, because a document readable by everyone
    would be visible to erin and 020_rls_test.sql asserts she sees nothing."""
    stack.exec(f"""
        insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
        select d.id, 'notices', false, 'Notices',
               (select p.id from wiki.principal p
                 where p.kind = 'user' and p.name = 'alice')
          from wiki.document d where d.path = 'root'::ltree;
        insert into wiki.document_version
               (document_id, version, path, content, message, author_id)
        select d.id, 1, d.path, '# Notice' || chr(10) || chr(10) || 'Public.',
               'initial', (select p.id from wiki.principal p
                            where p.kind = 'user' and p.name = 'alice')
          from wiki.document d where d.path = '{PUBLIC}'::ltree;
        insert into wiki.ace (document_id, principal_id, role_id, ace_type)
        select d.id,
               (select p.id from wiki.principal p
                 where p.kind = 'group' and p.name = 'public'),
               (select r.id from wiki.role r where r.name = 'reader'),
               'allow'
          from wiki.document d where d.path = '{PUBLIC}'::ltree;
    """)
    yield
    stack.exec(f"""
        delete from wiki.ace a using wiki.document d
         where a.document_id = d.id and d.path = '{PUBLIC}'::ltree;
        delete from wiki.document_version v using wiki.document d
         where v.document_id = d.id and d.path = '{PUBLIC}'::ltree;
        delete from wiki.document where path = '{PUBLIC}'::ltree;
    """)


# --- reading the token ------------------------------------------------------

def test_the_cookie_is_read_first():
    """Because that is what a browser will carry once there is a login, and the
    header is what a script uses. Neither should be the odd one out."""
    assert token_from([(b"cookie", f"{SESSION_COOKIE}=from-cookie".encode()),
                       (b"authorization", b"Bearer from-header")]) == "from-cookie"


def test_the_header_is_read_when_there_is_no_cookie():
    assert token_from([(b"authorization", b"Bearer abc")]) == "abc"


@pytest.mark.parametrize("headers", [
    [],
    [(b"authorization", b"Basic abc")],
    [(b"authorization", b"Bearer ")],
    [(b"cookie", b"other=1")],
    [(b"cookie", b"= malformed ;;")],
])
def test_no_token_is_None_rather_than_an_error(headers):
    """An anonymous visitor is an ordinary case, not a failure: they get
    whatever was granted to `public`."""
    assert token_from(headers) is None


# --- only reading -----------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_every_writing_method_is_refused(browser, method):
    r = await browser.request(method, "/")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET, HEAD"


async def test_the_refusal_comes_before_the_route_is_looked_at(browser):
    """Which is what makes read-only a property of the server rather than a
    claim about which routes exist today."""
    r = await browser.post("/-/no-such-thing-at-all")
    assert r.status_code == 405


async def test_head_carries_the_length_but_no_body(browser, published):
    r = await browser.head("/")
    assert r.status_code == 200
    assert r.content == b""
    assert int(r.headers["content-length"]) > 0


# --- what a visitor with no token gets --------------------------------------

async def test_an_anonymous_visitor_sees_the_public_page(browser, published):
    r = await browser.get("/")
    assert r.status_code == 200
    assert "notices" in r.text


async def test_and_not_the_rest_of_the_wiki(browser, published):
    r = await browser.get("/")
    assert "engineering" not in r.text


async def test_an_anonymous_visitor_can_read_it(browser, published):
    r = await browser.get("/notices")
    assert r.status_code == 200
    assert "Public." in r.text


async def test_a_page_they_may_not_read_is_a_page_that_is_not_there(browser):
    """The property the whole project turns on. Both pages echo the path that
    was asked for -- the visitor supplied it, so it discloses nothing -- and
    once that is substituted out, the two answers are the same bytes. Anything
    else in there would be the difference between "forbidden" and "missing",
    which is itself the disclosure."""
    refused = await browser.get("/engineering/secret-plans")
    missing = await browser.get("/engineering/no-such-page")
    assert refused.status_code == missing.status_code == 404
    assert (refused.text.replace("engineering/secret-plans", "X")
            == missing.text.replace("engineering/no-such-page", "X"))


# --- and with one -----------------------------------------------------------

async def test_a_token_in_the_header_is_that_person(browser, stack):
    r = await browser.get("/engineering/onboarding",
                          headers={"Authorization": f"Bearer {stack.token('bob')}"})
    assert r.status_code == 200


async def test_a_token_in_the_cookie_is_too(browser, stack):
    r = await browser.get(
        "/engineering/onboarding",
        headers={"Cookie": f"{SESSION_COOKIE}={stack.token('bob')}"})
    assert r.status_code == 200


async def test_the_browser_reads_what_a_mirror_may_not(browser, stack):
    """secret-plans carries a deny-sync ACE. Denying `sync` is meant to leave a
    page readable in a browser while keeping it off laptops, so this is the
    reader it was denied *for* -- and it is the whole reason the app asks for
    tree="read"."""
    r = await browser.get("/engineering/secret-plans",
                          headers={"Authorization": f"Bearer {stack.token('bob')}"})
    assert r.status_code == 200


async def test_a_token_the_wiki_will_not_take_says_so(browser):
    """Not a permission decision about a document -- those are already a 404 --
    but one about the token itself, which is fixable by signing in again."""
    r = await browser.get("/", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code in (401, 403)
    assert "did not accept" in r.text


# --- the headers ------------------------------------------------------------

@pytest.mark.parametrize("route", ["/", "/notices", "/no-such-page"])
async def test_every_response_carries_the_security_headers(browser, published, route):
    r = await browser.get(route)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in r.headers["content-security-policy"]


async def test_nothing_is_cached_because_a_304_is_an_unaudited_view(browser, published):
    """wiki.view_document() records the view in the transaction that serves the
    page. A browser answering from its own cache is a view nobody recorded."""
    r = await browser.get("/notices")
    assert r.headers["cache-control"] == "no-store"


# --- when the wiki is not there ---------------------------------------------

async def test_a_wiki_that_is_not_answering_is_a_502(config):
    """502 rather than 500: this server is fine and the one behind it is not."""
    broken = Config(database_url=config.database_url,
                    schema_dir=config.schema_dir,
                    postgrest_host="127.0.0.1", postgrest_port=1)
    app = Application(broken)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://wiki.test") as c:
        r = await c.get("/")
    assert r.status_code == 502
    assert "Cannot reach the wiki" in r.text
    await app.aclose()


# --- lifespan ---------------------------------------------------------------

async def test_the_pool_is_closed_on_shutdown(config):
    """Without a lifespan handler uvicorn logs that it is unsupported and the
    connections leak until the process dies."""
    app = Application(config)
    sent = []

    async def receive():
        return {"type": "lifespan.shutdown"} if sent else {"type": "lifespan.startup"}

    async def send(message):
        sent.append(message["type"])

    await app({"type": "lifespan"}, receive, send)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# --- the whole program ------------------------------------------------------
#
# Everything above drives the ASGI app directly, which is the right way to test
# routing and headers and says nothing about whether the three startup phases
# work together. This runs the real binary: it migrates (a no-op against a
# database that already has the schema), starts a PostgREST of its own, and
# serves. It is the only test that would notice the phases being reordered.

async def test_the_real_program_starts_and_serves(stack, published, tmp_path):
    import os
    import socket
    import subprocess

    from conftest import http, wait_for

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port, pgrst_port = free_port(), free_port()
    env = {
        **os.environ,
        "FSWIKI_DATABASE_URL":
            f"postgres://postgres@127.0.0.1:{stack.pg_port}/fswiki",
        # The shape a deployment uses: PostgREST connects as the authenticator,
        # which can do nothing itself, and not as the role that ran the DDL.
        "FSWIKI_POSTGREST_DATABASE_URL":
            f"postgres://fswiki_authenticator@127.0.0.1:{stack.pg_port}/fswiki",
        "FSWIKI_SCHEMA_DIR": str(ROOT / "server" / "schema"),
        "FSWIKI_HOST": "127.0.0.1",
        "FSWIKI_PORT": str(port),
        "FSWIKI_POSTGREST_PORT": str(pgrst_port),
        "FSWIKI_JWT_SECRET": stack.secret,
    }
    log = (tmp_path / "serve.log").open("wb")
    proc = subprocess.Popen(["fswiki-serve"], env=env, stdout=log, stderr=log)
    try:
        def answering():
            try:
                return http(f"http://127.0.0.1:{port}/", timeout=1)
            except OSError:
                return None

        try:
            index = wait_for(answering, what="fswiki-serve to answer")
        except AssertionError as exc:
            # Its own log is the only thing that can say why it never came up,
            # and a timeout that does not show it is a timeout you debug twice.
            raise AssertionError(
                f"{exc}\n--- fswiki-serve log ---\n"
                f"{(tmp_path / 'serve.log').read_text()}") from exc
        assert index.code == 200, index.body
        assert "notices" in index.body

        # And the identity plumbing all the way through: the token reaches
        # PostgREST, which sets the GUC, which RLS reads.
        page = http(f"http://127.0.0.1:{port}/engineering/onboarding",
                    token=stack.token("bob"))
        assert page.code == 200, page.body

        anonymous = http(f"http://127.0.0.1:{port}/engineering/onboarding")
        assert anonymous.code == 404, "anonymous read an engineering page"
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        log.close()
