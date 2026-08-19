"""The audit trail over HTTP.

Driven through PostgREST rather than through the mount, deliberately. The mount
caches bodies by version, so whether any given read is a real fetch depends on
what has been read before it — and the transport is what is under test here. It
should not be measured through a cache. The mount's own half is in
test_mount_audit.py.

Read docs/audit-trail.md first. The short version: this is telemetry, not
evidence. What the server knows by itself is the token, the document and the
time.
"""

from __future__ import annotations

import uuid

import pytest


def event(path: str, **extra) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
        "path": path,
        "occurred_at": "2026-08-18T00:00:00Z",
        "action": "open",
    }
    base.update(extra)
    return base


def test_a_read_is_recorded_by_the_request_that_serves_it(clean, rest):
    """One round trip. No flush involved, and nothing to lose if the client dies."""
    ev = event("root.public.archive.old-post")
    r = rest("/rpc/read_document", method="POST", body={
        "p_document": clean.doc("root.public.archive.old-post"), "p_event": ev})
    assert "content" in r
    assert clean.count(
        f"select count(*) from wiki.access_event where event_id = '{ev['event_id']}'") == 1


def test_read_document_is_a_second_way_to_read_not_a_second_answer(clean, rest):
    """SECURITY INVOKER over the same view, so it is exactly as filtered."""
    r = rest("/rpc/read_document", method="POST", body={
        "p_document": clean.doc("root.engineering.secret-plans")})
    assert r.body == "[]"


def test_the_server_takes_the_principal_from_the_token(clean, rest):
    ev = event("root.public.welcome")
    rest("/rpc/record_opens", method="POST", body={"p_events": [ev]})
    assert clean.scalar(
        "select p.name from wiki.access_event e "
        "join wiki.principal p on p.id = e.principal_id "
        f"where e.event_id = '{ev['event_id']}'") == "bob"


def test_a_user_cannot_file_events_against_someone_else(clean, rest):
    """A WITH CHECK failure, not a quiet success."""
    r = rest("/access_event", method="POST", body={
        "event_id": str(uuid.uuid4()),
        "principal_id": clean.who("frank"),
        "occurred_at": "2026-01-01T00:00:00Z",
        "action": "open",
    })
    assert r.code == 403


def test_nobody_reads_anybody_elses_trail(clean, rest):
    rest("/rpc/record_opens", method="POST", body={"p_events": [event("root.public.welcome")]})
    assert rest("/access_event?select=event_id", user="frank").json == []
    assert rest("/access_event?select=event_id", user="bob").json != []


@pytest.mark.parametrize("verb", ["PATCH", "DELETE"])
def test_the_trail_cannot_be_edited_by_its_subject(clean, rest, verb):
    """Withheld at the grant rather than by a policy, so it raises loudly
    instead of quietly filtering to zero rows."""
    rest("/rpc/record_opens", method="POST", body={"p_events": [event("root.public.welcome")]})
    r = rest("/access_event?action=eq.open", method=verb, body={"action": "open"})
    assert r.code >= 400
    assert clean.count("select count(*) from wiki.access_event") == 1


def test_a_resent_batch_adds_nothing(clean, rest):
    """The whole point of the client-generated event_id: delivery is
    at-least-once, so the duplicate must be a no-op rather than a second row."""
    ev = event("root.public.welcome")
    assert rest("/rpc/record_opens", method="POST", body={"p_events": [ev]}).json == 1
    assert rest("/rpc/record_opens", method="POST", body={"p_events": [ev]}).json == 0


def test_a_refused_read_is_recorded_without_a_document_id(clean, rest):
    """document_id is a foreign key, so a probe for an id that does not exist
    would otherwise abort the read it was attached to. The path in the payload
    is what identifies the attempt."""
    ev = event("root.secret.plans")
    rest("/rpc/read_document", method="POST", body={
        "p_document": "99999999-9999-9999-9999-999999999999", "p_event": ev})
    assert clean.count(
        "select count(*) from wiki.access_event "
        f"where event_id = '{ev['event_id']}' and document_id is null "
        "and path = 'root.secret.plans'") == 1


def test_the_process_claim_is_kept_as_given(clean, rest):
    """One jsonb column rather than columns per field: the shape is the
    client's, it will change, and promoting any of it would suggest the server
    believed it."""
    ev = event("root.public.welcome", process={"comm": "vim", "pid": 1234})
    rest("/rpc/record_opens", method="POST", body={"p_events": [ev]})
    assert clean.scalar(
        "select process ->> 'comm' from wiki.access_event "
        f"where event_id = '{ev['event_id']}'") == "vim"


def test_acted_as_is_null_for_an_ordinary_read(clean, rest):
    """It exists for impersonation and must not creep into normal reads, or
    the column stops meaning anything."""
    rest("/rpc/read_document", method="POST", body={
        "p_document": clean.doc("root.public.welcome"),
        "p_event": event("root.public.welcome")})
    assert clean.count("select count(*) from wiki.access_event where acted_as is not null") == 0
