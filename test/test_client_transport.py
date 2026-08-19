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

from fswiki_core.client import Client, PostgrestError, Unreachable

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
    """The property the whole poll design rests on: eleven bytes instead of
    six kilobytes, but only if an unchanged database answers the same way
    twice. See docs/change-notification.md."""
    bob = await client("bob")
    assert len({await bob.change_token() for _ in range(5)}) == 1


@pytest.mark.xfail(
    reason="wiki.changed() returns pg_current_wal_lsn(), and the impersonation "
           "hook UPDATEs impersonation_event (last_seen_at, requests) on every "
           "request -- so the token this client sees moves every single poll",
    strict=True)
async def test_the_impersonated_change_token_is_stable_too(granted, client):
    """`wiki.changed()` exists so an impersonated mount can poll cheaply. The
    SQL says so and calls it "not a nicety": without it a mount refetches the
    whole manifest every poll, "both a pointless six kilobytes and a steady
    drip into the log above".

    It does not work, and it cannot: the token is the WAL position, and the
    hook that authorises the impersonation writes to the log in the same
    breath. Measured here — five polls with nothing else touching the
    database give one token for an ordinary client and five distinct tokens
    for an impersonated one — so the impersonated mount does exactly what the
    function was added to prevent, and pays an extra round trip per poll to
    do it.

    Strict xfail: this is a schema fix, not a client one, and 075_changes.sql
    already names the shape of it — "replace the body with a counter bumped by
    statement-level triggers on document, document_version, ace, group_member
    and user_account". A counter over the tables that hold wiki content does
    not move when the audit log does. The signature stays, so no client
    changes.
    """
    as_bob = await client("dave", act_as=granted.who("bob"))
    assert len({await as_bob.change_token() for _ in range(5)}) == 1


async def test_an_ungranted_impersonation_is_refused_by_the_server(stack, client):
    """Locally there is nothing to check — the grant lives in the database, and
    the client asking politely is not what makes it safe."""
    frank = await client("frank", act_as=stack.who("bob"))
    with pytest.raises(PostgrestError):
        await frank.manifest()
