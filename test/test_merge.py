"""Three-way merge, end to end: bob edits, frank publishes, bob merges.

The merge base is a revision, not a timestamp, which is the whole reason the
draft records `base_version` — and the reason a lost update is detectable at
all. These tests set up the race deliberately rather than hoping for it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from conftest import wait_for

pytestmark = [
    pytest.mark.mount,
    pytest.mark.skipif(
        sys.platform == "darwin",
        reason="merge synchronization currently observes mount state through xattrs",
    ),
]

TARGET = "root.engineering.onboarding"
FILE = "engineering/onboarding.md"


@pytest.fixture
def publish_as_frank(stack, rest):
    """Move the server on behind bob's back.

    Over HTTP as frank rather than by writing rows directly: the point of the
    fixture is that another *client* published, and a hand-written row would
    skip the very code path that assigns the revision.
    """
    def do(content: str) -> int:
        body = {
            "author_id": stack.who("frank"), "operation": "update",
            "document_id": stack.doc(TARGET), "path": TARGET,
            "content": content, "base_version": stack.tip(TARGET),
        }
        r = rest("/draft?on_conflict=author_id,path", method="POST", user="frank",
                 body=body, headers={"Prefer": "resolution=merge-duplicates"})
        assert r.code < 400, r
        r = rest("/rpc/push", method="POST", user="frank", body={"p_message": "frank"})
        assert r.code < 400, r
        # Guard the guard: an empty revision here makes every merge look clean.
        assert stack.content(TARGET), "frank published an empty revision"
        return stack.tip(TARGET)
    return do


@pytest.fixture
def baseline(mount, clean, cli):
    """A five-line document, published, so the two sides can touch different parts."""
    def do() -> int:
        # Catch up first. The mount is session-scoped, so by the time a later
        # test runs the server is several revisions ahead of what this mount
        # last looked at, and the previous test's draft has been deleted from
        # under it by `clean`. A draft's base_version outranks everything else
        # when the next save picks a merge base, so a stale one would have this
        # baseline refused as a conflict before the test set anything up.
        notice(mount, clean.tip(TARGET))
        wait_for(lambda: xattr(mount, "state") == "published",
                 what="the mount to forget the previous test's draft")
        (mount / FILE).read_text()

        wait_for(lambda: _write(mount / FILE, "alpha\nbravo\ncharlie\ndelta\necho\n"),
                 what="the mount to accept the baseline")
        wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
                 what="the baseline draft")
        assert "Published" in cli("push", "-m", "baseline")
        version = clean.tip(TARGET)
        # Wait for the mount to catch up before handing the test back. Without
        # this, bob's first read is served from the cache at the *previous*
        # revision, so his checkout is one behind the baseline and every
        # assertion about the merge base is off by one for a reason that has
        # nothing to do with what is being tested.
        notice(mount, version)
        return version
    return do


def _write(path, text: str) -> bool:
    try:
        path.write_text(text)
        return True
    except OSError:
        return False


def xattr(mount, name: str) -> str | None:
    out = subprocess.run(
        ["getfattr", "-n", f"user.fswiki.{name}", "--only-values", str(mount / FILE)],
        capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def notice(mount, version: int) -> None:
    """Wait for the mount to have refreshed to `version` — without reading it.

    Reading the file is exactly what must not happen here. The mount tracks the
    revision it last *showed* this user, and that is what a later save records
    as its merge base. Re-reading the body after the other side published would
    quietly move bob's checkout forward and dismantle the race the test is
    setting up, leaving a test that passes because there was never a conflict.

    An xattr goes through lookup and getattr only, so the tree advances and the
    checkout does not.
    """
    wait_for(lambda: xattr(mount, "version") == str(version),
             what=f"the mount to notice revision {version}")


def edit(mount, clean, text: str) -> None:
    """Save `text` through the mount and wait for it to land as the draft.

    Waiting on the *content* rather than on a row count, because a save over an
    existing draft updates it in place — resolving a conflict by hand is
    exactly that, and counting rows would wait forever for a row that is never
    going to be inserted.
    """
    literal = "'" + text.replace("'", "''") + "'"
    wait_for(lambda: _write(mount / FILE, text), what="the mount to accept a write")
    wait_for(lambda: clean.count(
        f"select count(*) from wiki.draft "
        f"where path = '{TARGET}' and content = {literal}") == 1,
             what="the edit to become the draft")


# ---------------------------------------------------------------------------
# The lost update, which is what all of this exists to prevent
# ---------------------------------------------------------------------------

def test_a_draft_records_the_revision_that_was_actually_read(
        mount, clean, cli, baseline, publish_as_frank):
    """Recording the *current* revision instead would make the push succeed and
    destroy frank's work without telling anyone."""
    start = baseline()
    (mount / FILE).read_text()                    # bob checks out at `start`
    after = publish_as_frank("# frank was here\n")
    assert after == start + 1

    notice(mount, after)                          # the mount polls and refreshes
    edit(mount, clean, "# bob was editing all along\n")

    assert clean.count(
        f"select base_version from wiki.draft where path = '{TARGET}'") == start


def test_a_stale_push_is_refused_and_the_other_revision_survives(
        mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("# frank was here\n"))
    edit(mount, clean, "# bob was editing all along\n")

    assert "CONFLICT" in cli("push", "-m", "bob edit")
    assert clean.content(TARGET) == "# frank was here\n"


def test_re_reading_moves_the_base_forward_and_the_push_lands(
        mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    after = publish_as_frank("# frank was here\n")
    notice(mount, after)

    # The re-read is the point: it is what moves bob's checkout forward, and
    # so what makes the next save a descendant of frank's revision rather than
    # a competitor to it.
    (mount / FILE).read_text()
    edit(mount, clean, "# bob, after catching up\n")
    assert clean.count(
        f"select base_version from wiki.draft where path = '{TARGET}'") == after
    assert "Published 1 change" in cli("push", "-m", "rebased")


# ---------------------------------------------------------------------------
# Merging what can be merged
# ---------------------------------------------------------------------------

def test_edits_that_do_not_touch_merge_cleanly(
        mount, clean, cli, baseline, publish_as_frank):
    start = baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nbravo\ncharlie\ndelta\nECHO-frank\n"))
    edit(mount, clean, "ALPHA-bob\nbravo\ncharlie\ndelta\necho\n")

    r = cli("push", "-m", "bob")
    assert "CONFLICT" in r
    assert "merges cleanly" in r

    assert "Dry run" in cli("merge")
    assert clean.scalar("select content from wiki.draft").startswith("ALPHA-bob")

    assert "merged" in cli("merge", "--apply")
    merged = clean.scalar("select content from wiki.draft")
    assert "ALPHA-bob" in merged and "ECHO-frank" in merged

    assert clean.count("select merged_from from wiki.draft") == start + 1
    # base_version deliberately does not move at merge time. The rebase happens
    # when the merge is resolved, which for a clean merge is at push; until then
    # the draft still says what it was actually edited from.
    assert clean.count("select base_version from wiki.draft") == start

    assert "Published 1 change" in cli("push", "-m", "bob merged")


def test_both_sides_editing_one_line_does_not_auto_resolve(
        mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nBRAVO-frank\ncharlie\ndelta\necho\n"))
    edit(mount, clean, "alpha\nBRAVO-bob\ncharlie\ndelta\necho\n")

    assert "conflicting hunk" in cli("push", "-m", "bob")
    assert "CONFLICT" in cli("merge", "--apply")

    marked = clean.scalar("select content from wiki.draft")
    assert "<<<<<<<" in marked
    assert "BRAVO-bob" in marked and "BRAVO-frank" in marked


def test_push_refuses_text_that_is_still_marked(
        mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nBRAVO-frank\ncharlie\ndelta\necho\n"))
    edit(mount, clean, "alpha\nBRAVO-bob\ncharlie\ndelta\necho\n")
    cli("merge", "--apply")

    tip = clean.tip(TARGET)
    assert "unresolved conflict markers" in cli("push", "-m", "oops")
    assert clean.tip(TARGET) == tip, "the server tip must not have moved"


def test_resolving_by_hand_makes_it_publishable(
        mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nBRAVO-frank\ncharlie\ndelta\necho\n"))
    edit(mount, clean, "alpha\nBRAVO-bob\ncharlie\ndelta\necho\n")
    cli("merge", "--apply")

    edit(mount, clean, "alpha\nBRAVO-resolved\ncharlie\ndelta\necho\n")
    assert "Published 1 change" in cli("push", "-m", "resolved")
    assert "BRAVO-resolved" in clean.content(TARGET)


# ---------------------------------------------------------------------------
# The conflicted state itself
# ---------------------------------------------------------------------------

def test_the_server_refuses_a_conflicted_draft_whatever_the_client_believes(
        mount, clean, cli, rest, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nBRAVO-frank\ncharlie\ndelta\necho\n"))
    edit(mount, clean, "alpha\nBRAVO-bob\ncharlie\ndelta\necho\n")
    cli("merge", "--apply")

    r = rest("/rpc/push", method="POST", body={"p_message": "sneak"})
    assert '"status":"unmerged"' in r.body.replace(" ", "")
    assert clean.count("select count(*) from wiki.draft") == 1


def test_status_and_the_mount_both_say_it_is_conflicted(
        mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nBRAVO-frank\ncharlie\ndelta\necho\n"))
    edit(mount, clean, "alpha\nBRAVO-bob\ncharlie\ndelta\necho\n")
    cli("merge", "--apply")

    assert "unresolved merge" in cli("status")
    # A conflicted draft looks like any other file — the markers are in the
    # text and nothing else says so. The xattr is the only way the mount can
    # tell you, short of inventing a filename convention. The merge happened in
    # the CLI, so the mount learns of it on its next poll.
    wait_for(lambda: "conflicted" in (xattr(mount, "state") or ""),
             what="the mount to hear that the draft is conflicted")


def test_aborting_restores_byte_for_byte(mount, clean, cli, baseline, publish_as_frank):
    baseline()
    (mount / FILE).read_text()
    notice(mount, publish_as_frank("alpha\nBRAVO-frank\ncharlie\ndelta\necho\n"))
    edit(mount, clean, "alpha\nBRAVO-bob\ncharlie\ndelta\necho\n")
    mine = clean.scalar("select content from wiki.draft")

    cli("merge", "--apply")
    cli("merge", "--abort")

    assert clean.scalar("select content from wiki.draft") == mine
    assert clean.scalar("select state from wiki.draft") == "clean"
    assert clean.scalar("select pre_merge_content from wiki.draft") == ""
