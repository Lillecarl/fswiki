"""fswiki_core.client against a live PostgREST.

The one rule running through the module under test: **reads come from
`syncable_document`, never `current_document`**. The two differ exactly where a
deny-sync ACE sits, and the wrong one hands back content the server has said
must not be copied to a laptop. Most of what is below is that rule, checked
from a different angle each time.
"""

from __future__ import annotations

import uuid

import pytest

from fswiki_core.client import PostgrestError

pytestmark = pytest.mark.anyio


async def test_whoami_resolves_the_token(stack, client):
    c = await client("bob")
    assert await c.whoami() == stack.who("bob")


async def test_an_unknown_subject_resolves_to_nobody(stack, client):
    """Not an error: the token verified. It just is not anyone here.

    Worth its own test because the mount turns exactly this into "your token
    expired" rather than into an empty wiki, and only if it comes back None.
    """
    c = await client("nobody-in-particular")
    assert await c.whoami() is None


async def test_manifest_is_one_round_trip_with_everything_stat_needs(client):
    c = await client("bob")
    rows = await c.manifest()
    assert rows
    row = rows[0]
    for column in ("id", "path", "is_folder", "version", "size",
                   "content_type", "capabilities"):
        assert column in row, f"the manifest must carry {column} or stat() must ask again"


async def test_manifest_is_ordered_by_path(client):
    c = await client("bob")
    paths = [r["path"] for r in await c.manifest()]
    assert paths == sorted(paths)


async def test_manifest_is_filtered_per_reader(client):
    bob = await client("bob")
    dave = await client("dave")
    mine = {r["path"] for r in await bob.manifest()}
    theirs = {r["path"] for r in await dave.manifest()}
    # bob is an editor across engineering; dave is in `everyone` only.
    assert "root.engineering.onboarding" in mine
    assert "root.engineering.onboarding" not in theirs
    # But not nothing: `private` blocks inheritance and grants `everyone`
    # reader, so dave sees straight through the subtree he is otherwise denied.
    # A test asserting he sees no engineering path at all would be asserting a
    # bug.
    assert "root.engineering.private.memo" in theirs


async def test_content_comes_back_as_bytes(stack, client):
    c = await client("bob")
    body = await c.content(stack.doc("root.public.welcome"))
    assert isinstance(body, bytes) and body


async def test_a_document_that_is_readable_but_not_syncable_is_absent(stack, client):
    """The deny-sync ACE, which is the whole reason for the syncable view.

    bob may *read* secret-plans; he may not mirror it. If this ever returns
    content, the client has started using current_document somewhere.
    """
    c = await client("bob")
    with pytest.raises(LookupError):
        await c.content(stack.doc("root.engineering.secret-plans"))


async def test_document_by_path_conflates_forbidden_with_missing(client):
    """Which is the answer a renderer wants: telling them apart leaks the graph."""
    c = await client("bob")
    assert await c.document("root.engineering.secret-plans") is None
    assert await c.document("root.public.no-such-page") is None


async def test_document_by_path_returns_what_the_renderer_needs(client):
    c = await client("bob")
    row = await c.document("root.public.welcome")
    assert row and set(row) >= {"id", "path", "content", "content_type", "version"}


async def test_change_token_moves_only_when_something_is_written(stack, client, clean):
    c = await client("bob")
    before = await c.change_token()
    assert before
    assert await c.change_token() == before          # a read moves nothing
    stack.exec("insert into wiki.principal (kind, name) "
               "values ('group', 'token-probe-" + uuid.uuid4().hex[:8] + "')")
    assert await c.change_token() != before


async def test_a_bad_token_raises_rather_than_returning_nothing(stack):
    """An expired token must not look like an empty wiki."""
    from fswiki_core.client import Client

    c = Client(stack.url, "not.a.token")
    try:
        with pytest.raises(PostgrestError):
            await c.whoami()
    finally:
        await c.aclose()


async def test_content_with_an_event_records_it_in_the_same_breath(stack, client, clean):
    """POST rather than GET, and the verb is the point.

    PostgREST runs GET in a read-only transaction, so a GET cannot write its own
    audit row. This asserts the row is there when the bytes arrive — not after a
    flush, which would be a different guarantee.
    """
    c = await client("bob")
    event_id = str(uuid.uuid4())
    body = await c.content(stack.doc("root.public.welcome"), event={
        "event_id": event_id,
        "path": "root.public.welcome",
        "occurred_at": "2026-08-18T00:00:00Z",
        "action": "open",
    })
    assert body
    assert stack.count(
        f"select count(*) from wiki.access_event where event_id = '{event_id}'") == 1


async def test_reading_without_an_event_records_nothing(stack, client, clean):
    c = await client("bob")
    await c.content(stack.doc("root.public.welcome"))
    assert stack.count("select count(*) from wiki.access_event") == 0


async def test_a_refused_read_is_still_recorded(stack, client, clean):
    """A request for what you may not have is the more interesting half of a log."""
    c = await client("bob")
    event_id = str(uuid.uuid4())
    with pytest.raises(LookupError):
        await c.content(stack.doc("root.engineering.secret-plans"), event={
            "event_id": event_id,
            "path": "root.engineering.secret-plans",
            "occurred_at": "2026-08-18T00:00:00Z",
            "action": "open",
        })
    assert stack.count(
        f"select count(*) from wiki.access_event where event_id = '{event_id}'") == 1


async def test_record_opens_is_idempotent(stack, client, clean):
    """The queue resends batches it never saw acknowledged; that must be free."""
    c = await client("bob")
    event = {
        "event_id": str(uuid.uuid4()),
        "path": "root.public.welcome",
        "occurred_at": "2026-08-18T00:00:00Z",
        "action": "open",
    }
    assert await c.record_opens([event]) == 1
    assert await c.record_opens([event]) == 0
