"""How the client talks, and how it fails.

Three things live here that nothing else checks in process. The transport
choice: while impersonating, every read goes over a volatile RPC rather than a
GET, because PostgREST runs a GET in a read-only transaction and impersonation
refuses any transaction it cannot write its own log into. The error type: what
a caller can rely on being able to ask a `PostgrestError`. And `Unreachable`,
which is not an OSError — a fact that once let a refused connection out of a
FUSE handler as a traceback and took the whole mount down with it.
"""

from __future__ import annotations

import httpx
import pytest

from fswiki_core.client import Client, ClientPool, PostgrestError, Unreachable

pytestmark = pytest.mark.anyio

TARGET = "root.engineering.onboarding"


@pytest.fixture
def granted(clean):
    """dave may act as bob. Idempotent: a grant is configuration, not state,
    and `clean` deliberately leaves it alone."""
    clean.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'), p.id
            from wiki.principal p where p.name in ('everyone', 'engineering')
        on conflict do nothing;
    """)
    return clean


# --- the error type --------------------------------------------------------

def test_a_postgrest_error_carries_the_status_a_caller_switches_on():
    """`_errno_for` in the mount asks for exactly this to decide between
    EACCES and EIO, and it asks it in a FUSE handler where being wrong
    unmounts the filesystem."""
    request = httpx.Request("GET", "http://wiki.example/document")
    exc = PostgrestError(httpx.Response(403, json={"message": "denied"},
                                        request=request))
    assert exc.status == 403
    assert exc.body["message"] == "denied"
    assert "denied" in str(exc)
    assert "403" in str(exc)


def test_an_error_body_that_is_not_json_is_kept_as_text():
    """A proxy in front of PostgREST returns HTML, and losing the body to a
    JSONDecodeError would leave the user with a bare status code."""
    request = httpx.Request("GET", "http://wiki.example/document")
    exc = PostgrestError(httpx.Response(502, text="<html>bad gateway</html>",
                                        request=request))
    assert exc.body == "<html>bad gateway</html>"
    assert "bad gateway" in str(exc)


def test_an_error_is_a_runtime_error_not_an_os_error():
    """Both halves matter. `except OSError` must not swallow it, and neither
    the CLI nor the mount may let it out uncaught."""
    request = httpx.Request("GET", "http://wiki.example/")
    exc = PostgrestError(httpx.Response(500, text="", request=request))
    assert isinstance(exc, RuntimeError)
    assert not isinstance(exc, OSError)


def test_unreachable_is_not_an_os_error_either():
    """The trap this alias exists to mark. httpx does not derive from OSError,
    so `except OSError` alone lets a refused connection out as a traceback —
    which is exactly what it did, out of a FUSE handler, until a test asked
    what happens when the server is down."""
    assert not issubclass(Unreachable, OSError)
    assert issubclass(httpx.ConnectError, Unreachable)


def test_unreachable_has_no_status_to_ask_for():
    """The second bug the first fix exposed: catching it alongside
    PostgrestError and then reading `.status` is an AttributeError out of
    flush(), which is no better than the crash it replaced."""
    assert not hasattr(Unreachable("nothing there"), "status")


# --- reaching nothing ------------------------------------------------------

async def test_a_server_that_is_not_there_raises_unreachable(stack):
    """Port 1 is reserved and nothing listens on it."""
    c = Client("http://127.0.0.1:1", stack.token("bob"))
    try:
        with pytest.raises(Unreachable):
            await c.whoami()
    finally:
        await c.aclose()


async def test_the_address_is_available_for_saying_which_one(stack):
    """Every caller that reports "cannot reach it" wants to name the address,
    and the preview server puts it on a 502 page."""
    c = Client("http://127.0.0.1:1/", stack.token("bob"))
    try:
        assert c.base_url == "http://127.0.0.1:1"
    finally:
        await c.aclose()


async def test_a_token_the_server_will_not_take_raises_rather_than_emptying(stack):
    """An empty wiki and a rejected token look identical to a user, and only
    one of them is fixable by logging in again."""
    c = Client(stack.url, "not-a-jwt")
    try:
        with pytest.raises(PostgrestError) as caught:
            await c.change_token()
        assert caught.value.status in (401, 403)
    finally:
        await c.aclose()


# --- an empty body ---------------------------------------------------------

def test_no_content_is_no_rows_rather_than_a_parse_error():
    """PostgREST answers 204 with an empty body for a write that returns
    nothing, and json() on that raises."""
    request = httpx.Request("DELETE", "http://wiki.example/draft")
    assert Client._rows(httpx.Response(204, request=request)) == []


def test_a_single_object_is_wrapped_as_one_row():
    """Some routes answer with an object rather than an array, and every
    caller here indexes."""
    request = httpx.Request("GET", "http://wiki.example/draft")
    assert Client._rows(httpx.Response(200, json={"path": "root.a"},
                                       request=request)) == [{"path": "root.a"}]


def test_a_failure_raises_before_anything_tries_to_read_rows():
    request = httpx.Request("GET", "http://wiki.example/draft")
    with pytest.raises(PostgrestError):
        Client._rows(httpx.Response(403, json={"message": "no"}, request=request))


# --- the transport impersonation forces ------------------------------------

async def test_an_ordinary_client_is_not_impersonating(stack, client):
    c = await client("bob")
    assert not c.impersonating


async def test_acting_as_a_person_reads_what_they_read(granted, client):
    """The header is what changed the answer, so the same request without it
    must come back empty or this proves nothing."""
    dave = await client("dave")
    assert await dave.document(TARGET) is None

    as_bob = await client("dave", act_as=granted.who("bob"))
    assert as_bob.impersonating
    assert (await as_bob.document(TARGET))["path"] == TARGET


async def test_acting_as_a_membership_reads_what_a_member_would(granted, client):
    """Not a member — the membership itself. See docs/impersonation.md for why
    those are different questions."""
    as_engineering = await client("dave",
                                  act_as_groups=[granted.who("engineering")])
    assert as_engineering.impersonating
    assert await as_engineering.document(TARGET) is not None


async def test_every_read_still_works_over_the_volatile_transport(granted, client):
    """The whole read surface, not just one route: a GET runs read-only and
    impersonation refuses that, so each of these takes a different path
    through `_reading` and any one of them could have been missed."""
    as_bob = await client("dave", act_as=granted.who("bob"))
    assert await as_bob.whoami() == granted.who("bob")
    assert await as_bob.manifest()
    assert await as_bob.drafts() is not None
    assert await as_bob.content(granted.doc(TARGET))
    assert isinstance(await as_bob.change_token(), str)


async def test_the_change_token_is_stable_when_nothing_changes(stack, client):
    """The property the whole poll design rests on: a few bytes instead of
    six kilobytes, but only if an unchanged database answers the same way
    twice. See docs/change-notification.md."""
    bob = await client("bob")
    assert len({await bob.change_token() for _ in range(5)}) == 1


async def test_the_impersonated_change_token_is_stable_too(granted, client):
    """`wiki.changed()` exists so an impersonated mount can poll cheaply, and
    for a while it could not: the token was `pg_current_wal_lsn()`, and the
    hook that authorises the impersonation UPDATEs its session row in the same
    breath, so the WAL moved on every single poll. An impersonated mount
    refetched the whole manifest every time and paid an extra round trip to do
    it. See 075_changes.sql, which is now a counter over the tables that hold
    what a client can see."""
    as_bob = await client("dave", act_as=granted.who("bob"))
    assert len({await as_bob.change_token() for _ in range(5)}) == 1


async def test_the_token_still_moves_when_something_actually_changes(
        granted, client, stack):
    """The other half, and the half a broken fix would pass: a token that
    never moves is cheap and useless. It must be stable *and* still notice."""
    as_bob = await client("dave", act_as=granted.who("bob"))
    before = await as_bob.change_token()
    stack.exec("update wiki.document set title = title "
               "where path = 'root.engineering.onboarding'")
    assert await as_bob.change_token() != before


async def test_the_audit_trail_was_not_what_paid_for_it(granted, client):
    """The cheap way to stop the token moving would have been to make the hook
    write less often, and that would have bought a poll optimisation with the
    record of who acted as whom. It did not: every impersonated request is
    still counted, the writes simply no longer look like content changes."""
    as_bob = await client("dave", act_as=granted.who("bob"))
    before = granted.count(
        "select coalesce(sum(requests), 0) from wiki.impersonation_event")
    for _ in range(5):
        await as_bob.change_token()
    after = granted.count(
        "select coalesce(sum(requests), 0) from wiki.impersonation_event")
    assert after - before == 5


async def test_an_ungranted_impersonation_is_refused_by_the_server(stack, client):
    """Locally there is nothing to check — the grant lives in the database, and
    the client asking politely is not what makes it safe."""
    frank = await client("frank", act_as=stack.who("bob"))
    with pytest.raises(PostgrestError):
        await frank.manifest()


# --- the connection pool ---------------------------------------------------
#
# `Client` is one identity and, by default, connections of its own. A server
# renders for whoever is asking, so the token changes per request and a client
# per request would open a fresh connection to PostgREST for every page view.
# `ClientPool` exists to separate the two: headers stay per-identity, sockets
# are shared. The hazard the tests below pin down is the seam between them —
# httpx.AsyncClient.aclose() closes its transport unconditionally, so a
# borrower that closes normally would take everyone else's connections with it.


class _CountingTransport(httpx.AsyncBaseTransport):
    """A transport that answers nothing and remembers being closed."""

    def __init__(self) -> None:
        self.closes = 0
        self.tokens: list[str | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=[])

    async def aclose(self) -> None:
        self.closes += 1


def test_a_pool_hands_every_identity_the_same_connections():
    pool = ClientPool()
    a = pool.client("http://example.invalid", "token-a")
    b = pool.client("http://example.invalid", "token-b")
    assert a._http._transport is b._http._transport


async def test_closing_a_borrowed_client_leaves_the_pool_open():
    """The whole point of the split. Under concurrency this bug looks like the
    server hanging up at random, because it is one request's cleanup reaching
    into every other request's sockets."""
    shared = _CountingTransport()
    c = Client("http://example.invalid", "a-token", transport=shared)
    await c.whoami()
    await c.aclose()
    assert shared.closes == 0


async def test_a_client_with_connections_of_its_own_still_closes_them():
    """The other half: nothing above changes for the CLI or the mount, which
    own their pool and must still release it when they stop."""
    c = Client("http://example.invalid", "a-token")
    await c.aclose()
    assert c._http.is_closed


async def test_identities_sharing_a_pool_do_not_share_a_token():
    """Sockets are identity-agnostic; headers are the identity. If sharing a
    pool ever leaked a token between clients, RLS would be answering the wrong
    question with a completely straight face."""
    shared = _CountingTransport()
    a = Client("http://example.invalid", "token-a", transport=shared)
    b = Client("http://example.invalid", "token-b", transport=shared)
    await a.whoami()
    await b.whoami()
    await a.whoami()
    assert shared.tokens == ["Bearer token-a", "Bearer token-b", "Bearer token-a"]


async def test_two_people_on_one_pool_get_their_own_answers(stack):
    """The same thing against a real PostgREST, because the header plumbing
    that matters is the plumbing httpx actually sends."""
    pool = ClientPool()
    try:
        bob = pool.client(stack.url, stack.token("bob"))
        carol = pool.client(stack.url, stack.token("carol"))
        assert await bob.whoami() != await carol.whoami()
    finally:
        await pool.aclose()
