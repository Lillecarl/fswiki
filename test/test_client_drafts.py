"""The write half of fswiki_core.client, against a live PostgREST.

test_client.py covers reading, where the rule is "never `current_document`".
This is the other half: drafts, push, revisions and the merge RPCs. Every one
of them is reached today only by running the CLI as a subprocess and matching
a substring of what it printed, which tests the substring — it says nothing
about what `delete_draft` returns when the draft was someone else's, and that
is a case where returning the wrong thing is silent.

Two answers here are deliberately not exceptions, and both are the ones worth
being sure about: RLS *filters*, so an operation on a row that is not yours
comes back as no rows rather than as a refusal. Never read "no error" as "it
worked".
"""

from __future__ import annotations

import pytest

from fswiki_core.client import PostgrestError

pytestmark = pytest.mark.anyio

TARGET = "root.engineering.onboarding"


@pytest.fixture
async def bob(stack, client, clean):
    c = await client("bob")
    c.author = stack.who("bob")
    return c


async def save(c, stack, *, path=TARGET, operation="update", content="edited\n",
               **extra):
    return await c.put_draft(
        author_id=c.author, operation=operation, path=path,
        document_id=stack.doc(path) if operation != "create" else None,
        content=content,
        base_version=stack.tip(path) if operation != "create" else None,
        **extra)


# --- drafts ----------------------------------------------------------------

async def test_a_saved_draft_comes_back_with_what_the_status_report_needs(bob, stack):
    row = await save(bob, stack)
    assert row["path"] == TARGET
    listed = await bob.drafts()
    assert len(listed) == 1
    for column in ("path", "operation", "content", "base_version", "state"):
        assert column in listed[0], f"status cannot be rendered without {column}"


async def test_saving_twice_replaces_rather_than_duplicates(bob, stack):
    """Upserted on (author_id, path), not on the primary key, because the
    client thinks in paths and never learns a draft's id. Without that, every
    save through the mount would leave another row behind."""
    await save(bob, stack, content="first\n")
    await save(bob, stack, content="second\n")
    listed = await bob.drafts()
    assert len(listed) == 1
    assert listed[0]["content"] == "second\n"


async def test_a_draft_is_private_to_its_author(bob, stack, client):
    await save(bob, stack)
    frank = await client("frank")
    assert await frank.drafts() == []


async def test_a_content_type_is_sent_only_when_there_is_one(bob, stack):
    """The column has a default, and sending an explicit null would override
    it with nothing rather than letting the server choose."""
    plain = await save(bob, stack, path="root.engineering.notes-x",
                       operation="create", content="x\n")
    assert plain["content_type"]
    typed = await save(bob, stack, path="root.engineering.notes-y",
                       operation="create", content="{}\n",
                       content_type="application/json")
    assert typed["content_type"] == "application/json"


async def test_deleting_your_own_draft_says_it_deleted_one(bob, stack):
    await save(bob, stack)
    assert await bob.delete_draft(TARGET) is True
    assert await bob.drafts() == []


async def test_deleting_a_draft_that_is_not_there_says_so(bob):
    assert await bob.delete_draft(TARGET) is False


async def test_deleting_someone_elses_draft_is_not_an_error_and_not_a_success(
        bob, stack, client):
    """The case this method's return value exists for. RLS filters, so the row
    is simply not among those deleted and PostgREST reports a perfectly
    successful DELETE of nothing. A client that trusted the status code would
    tell the user their draft was withdrawn while it sat there."""
    frank = await client("frank")
    frank_id = stack.who("frank")
    await frank.put_draft(author_id=frank_id, operation="update", path=TARGET,
                          document_id=stack.doc(TARGET), content="frank's\n",
                          base_version=stack.tip(TARGET))
    try:
        assert await bob.delete_draft(TARGET) is False
        assert len(await frank.drafts()) == 1
    finally:
        await frank.delete_draft(TARGET)


# --- push ------------------------------------------------------------------

async def test_push_publishes_and_reports_the_new_revision(bob, stack):
    before = stack.tip(TARGET)
    await save(bob, stack, content="published by the client\n")
    rows = await bob.push("from the client")
    assert [r["status"] for r in rows] == ["published"]
    assert rows[0]["version"] == before + 1
    assert stack.content(TARGET) == "published by the client\n"


async def test_push_with_nothing_pending_returns_no_rows(bob):
    assert await bob.push("nothing") == []


async def test_push_can_be_given_a_subset(bob, stack):
    await save(bob, stack, content="this one\n")
    await save(bob, stack, path="root.engineering.notes-subset",
               operation="create", content="not this one\n")
    rows = await bob.push("subset", paths=[TARGET])
    assert [r["path"] for r in rows] == [TARGET]
    assert [d["path"] for d in await bob.drafts()] == ["root.engineering.notes-subset"]


async def test_a_stale_draft_is_refused_and_every_row_says_so(bob, stack, client):
    """All or nothing: one bad row means nothing was written. The client must
    look at every row, so push must return every row."""
    await save(bob, stack, content="mine\n")
    frank = await client("frank")
    await frank.put_draft(author_id=stack.who("frank"), operation="update",
                          path=TARGET, document_id=stack.doc(TARGET),
                          content="frank got there first\n",
                          base_version=stack.tip(TARGET))
    await frank.push("frank")

    rows = await bob.push("mine")
    assert [r["status"] for r in rows] == ["conflict"]
    assert rows[0]["server_content"] == "frank got there first\n"
    assert rows[0]["base_content"] is not None
    assert await bob.drafts(), "a refused push must leave the draft alone"


# --- revisions -------------------------------------------------------------

async def test_a_closed_revision_is_readable_because_it_is_the_merge_base(
        bob, stack):
    """The tip views deliberately show only the open revision, and the
    ancestor an edit descends from is by definition closed — so this cannot go
    through them."""
    original = stack.content(TARGET)
    base = stack.tip(TARGET)
    await save(bob, stack, content="a new tip\n")
    await bob.push("move the tip")

    assert stack.tip(TARGET) == base + 1
    assert await bob.revision(stack.doc(TARGET), base) == original


async def test_a_revision_that_never_existed_is_none_not_an_error(bob, stack):
    assert await bob.revision(stack.doc(TARGET), 9999) is None


# --- the merge RPCs --------------------------------------------------------

async def test_beginning_a_merge_keeps_the_text_it_replaced(bob, stack):
    """Which is the whole reason abort can exist at all."""
    await save(bob, stack, content="mine\n")
    row = await bob.begin_merge(TARGET, "merged\n", stack.tip(TARGET),
                                conflicted=False)
    assert row["content"] == "merged\n"
    assert row["pre_merge_content"] == "mine\n"
    assert row["state"] != "conflicted"


async def test_a_conflicted_merge_is_marked_as_one(bob, stack):
    """Push keys on the state, not on the text, so a client that merged
    without saying so could publish marked content."""
    await save(bob, stack, content="mine\n")
    row = await bob.begin_merge(TARGET, "<<<<<<< yours\n", stack.tip(TARGET),
                                conflicted=True)
    assert row["state"] == "conflicted"
    assert row["merged_from"] == stack.tip(TARGET)


async def test_resolving_rebases_and_drops_the_backup(bob, stack):
    await save(bob, stack, content="mine\n")
    tip = stack.tip(TARGET)
    await bob.begin_merge(TARGET, "merged\n", tip, conflicted=True)
    row = await bob.resolve_merge(TARGET)
    assert row["base_version"] == tip
    assert row["pre_merge_content"] is None
    assert row["state"] != "conflicted"


async def test_aborting_puts_the_text_back_byte_for_byte(bob, stack):
    await save(bob, stack, content="mine\n")
    await bob.begin_merge(TARGET, "merged\n", stack.tip(TARGET), conflicted=True)
    row = await bob.abort_merge(TARGET)
    assert row["content"] == "mine\n"
    assert row["pre_merge_content"] is None


async def test_a_merge_rpc_on_a_draft_that_is_not_yours_returns_nothing(
        bob, stack, client):
    """406, because the RPC asks for exactly one object and RLS filtered the
    row away. None rather than an exception, and the caller must not read it
    as success — the draft was not theirs to rewrite."""
    frank = await client("frank")
    await frank.put_draft(author_id=stack.who("frank"), operation="update",
                          path=TARGET, document_id=stack.doc(TARGET),
                          content="frank's\n", base_version=stack.tip(TARGET))
    try:
        assert await bob.begin_merge(TARGET, "x\n", 1, conflicted=False) is None
        assert await bob.resolve_merge(TARGET) is None
        assert await bob.abort_merge(TARGET) is None
        assert (await frank.drafts())[0]["content"] == "frank's\n"
    finally:
        await frank.delete_draft(TARGET)


async def test_a_merge_rpc_on_a_path_with_no_draft_returns_nothing(bob):
    assert await bob.resolve_merge("root.nothing.here") is None


# --- what a client may not do ----------------------------------------------

async def test_a_draft_cannot_be_filed_against_someone_else(bob, stack):
    """The author_id is sent by the client, so the server has to be the one
    that refuses. If this ever stops raising, anyone can put words in anyone's
    mouth."""
    with pytest.raises(PostgrestError):
        await bob.put_draft(author_id=stack.who("frank"), operation="update",
                            path=TARGET, document_id=stack.doc(TARGET),
                            content="not mine to write\n",
                            base_version=stack.tip(TARGET))


async def test_acting_as_a_person_and_a_membership_at_once_is_refused_locally(
        stack, client):
    """Before the network, because the two mean different things and the
    server would have to guess which was meant. See docs/impersonation.md."""
    with pytest.raises(ValueError, match="not both"):
        await client("bob", act_as=stack.who("frank"),
                     act_as_groups=[stack.who("everyone")])
