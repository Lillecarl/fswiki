"""What the mount does when the server goes away.

A filesystem is not a request. If a client library raises somewhere a browser
would show an error page, the user retries; if it raises inside a FUSE handler
the exception comes out of `pyfuse3.main()` and takes the whole mount down,
leaving a mountpoint that hangs every `ls` until someone unmounts it by hand.
So the bar here is higher than "reports an error": nothing the network does may
end the process.

There are two outages here and they are not the same one. Killing PostgREST
means every request is a connection refused, which arrives as a transport error
and never touches the response-handling code at all. Killing the *database*
under a live PostgREST means every request gets an answer -- a 503, with a
schema cache it cannot load -- which arrives through the ordinary error path
with a status nothing has a branch for. A client can handle either one and drop
the other.

The first uses a PostgREST of its own, because the interesting move is killing
it. The second stops the session's cluster and always brings it back.
"""

from __future__ import annotations

import errno
import os

import pytest

from conftest import wait_for

pytestmark = pytest.mark.mount


@pytest.fixture
def blinkable(spare, mount_factory, tmp_path):
    """An ordinary mount pointed at a server this test may take away."""
    return mount_factory("--url", spare.url, "--poll", "0.5")


def alive(mount) -> bool:
    return mount.proc.poll() is None


def test_the_mount_survives_the_server_going_away(blinkable, spare, clean):
    """The bug this test exists for: `httpx.ConnectError` is not an `OSError`
    and not a `PostgrestError`, so a handler that caught both still let it out
    — and out of a FUSE handler means the end of the filesystem, not the end of
    the syscall."""
    (blinkable / "public/welcome.md").read_text()
    spare.stop()

    with pytest.raises(OSError):
        # Uncached, so it has to go to a server that is not there.
        (blinkable / "public/guide/pushing.md").read_text()

    assert alive(blinkable), "a failed read killed the mount"


def test_a_failed_read_is_an_io_error_and_not_a_missing_file(blinkable, spare):
    """ENOENT would be a lie with consequences: `cp -r` would carry on happily
    and produce a copy with holes in it, and rsync would delete the
    destination's copy of a file that still exists."""
    (blinkable / "public/welcome.md").read_text()
    spare.stop()

    with pytest.raises(OSError) as exc:
        (blinkable / "public/guide/pushing.md").read_text()
    assert exc.value.errno == errno.EIO


def test_the_tree_survives_a_poll_that_fails(blinkable, spare):
    """The poller runs in a task group beside `pyfuse3.main()`, so an exception
    there is just as fatal. `ls` must keep working from the last good tree —
    blanking the mount because the network blinked would look exactly like
    every document having been deleted."""
    before = sorted(os.listdir(blinkable.path / "public"))
    spare.stop()

    for _ in range(4):  # several poll intervals
        assert sorted(os.listdir(blinkable.path / "public")) == before
        assert alive(blinkable)


def test_what_was_already_read_still_reads(blinkable, spare):
    """Reading through the mount works with no network. That is the promise the
    content cache makes, and it is most of why the mount is worth having on a
    laptop at all."""
    body = (blinkable / "public/welcome.md").read_text()
    spare.stop()
    assert (blinkable / "public/welcome.md").read_text() == body


def test_a_write_that_cannot_be_saved_fails_the_write_not_the_mount(
        blinkable, spare, clean):
    """A draft is a row on the server, so there is nowhere to put this. The
    editor has to hear about it — an error at save time is recoverable, a
    filesystem that vanished mid-save is not."""
    (blinkable / "engineering/onboarding.md").read_text()
    spare.stop()

    with pytest.raises(OSError):
        (blinkable / "engineering/onboarding.md").write_text("offline edit\n")

    assert alive(blinkable)


def test_it_picks_up_again_by_itself(blinkable, spare, clean):
    """No remount, no command. The poll that failed is the poll that recovers,
    which is what makes closing a laptop a non-event."""
    (blinkable / "public/welcome.md").read_text()
    spare.stop()
    with pytest.raises(OSError):
        (blinkable / "public/guide/pushing.md").read_text()

    spare.start()
    assert wait_for(lambda: _readable(blinkable / "public/guide/pushing.md"),
                    timeout=30, what="the mount to start serving again")


def test_a_write_lands_once_the_server_is_back(blinkable, spare, clean):
    (blinkable / "engineering/onboarding.md").read_text()
    spare.stop()
    with pytest.raises(OSError):
        (blinkable / "engineering/onboarding.md").write_text("during the outage\n")

    spare.start()
    wait_for(lambda: _writable(blinkable / "engineering/onboarding.md",
                               "after the outage\n"),
             timeout=30, what="the mount to accept writes again")
    assert wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
                    what="the draft to land")


def _readable(path) -> bool:
    try:
        path.read_text()
        return True
    except OSError:
        return False


def _writable(path, text: str) -> bool:
    try:
        path.write_text(text)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# One layer down: PostgREST is up, the database is not
#
# A different failure from the one above, and clients meet the two through
# completely different code paths. Here every request gets an answer -- 503,
# with a schema cache PostgREST cannot load -- so nothing raises a transport
# error and nothing is refused by the ACL either.
# ---------------------------------------------------------------------------

def test_the_mount_survives_the_database_going_away(mount, db_outage, clean):
    """`ls` still works and the process is still there. A 503 is a
    `PostgrestError` like any other, which is the point: the interesting risk
    is a status nothing has a branch for arriving somewhere that assumed one."""
    before = sorted(os.listdir(mount.path / "public"))
    db_outage.stop()

    assert sorted(os.listdir(mount.path / "public")) == before
    assert alive(mount)


def test_a_read_during_a_database_outage_is_an_io_error(mount, db_outage):
    """Not EACCES. `_errno_for` maps 401 and 403 to "you may not" and
    everything else to "it went wrong", and a database that is down must land
    in the second: telling someone they lack permission when the truth is that
    nothing is running sends them to an administrator about the wrong thing."""
    (mount / "public/welcome.md").read_text()
    db_outage.stop()

    with pytest.raises(OSError) as exc:
        (mount / "public/archive/old-post.md").read_text()
    assert exc.value.errno == errno.EIO


def test_the_mount_recovers_when_the_database_comes_back(mount, db_outage, clean):
    db_outage.stop()
    with pytest.raises(OSError):
        (mount / "public/archive/old-post.md").read_text()

    db_outage.start()
    assert wait_for(lambda: _readable(mount / "public/archive/old-post.md"),
                    timeout=60, what="the mount to serve again")


def test_the_cli_reports_a_database_outage_as_a_sentence(cli, db_outage):
    """503 arrives with a body PostgREST wrote, not a JSON error object of the
    shape the client expects. Reading a message out of it must not be what
    turns an outage into a traceback."""
    db_outage.stop()
    r = cli("status")
    assert r.code == 1
    assert "Traceback" not in r
