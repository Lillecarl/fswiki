"""`fswiki-mount --audit`: what the mount records about opening a file.

The other half of the trail is in test_audit.py, which drives PostgREST
directly. This half is the part that only a real filesystem can show: who the
caller was, which route the event took to the server, and what happens to it
when the server is not there.

Read docs/audit-trail.md first. Two things the tests below keep returning to:

- **The fetch carries the event.** A read and the record of it commit together
  or neither does, so the ordinary case involves no queue at all.
- **The cache is why the queue still exists.** A second open of the same
  revision is served locally, the server never hears about it, and the spool is
  the only route left.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import wait_for

pytestmark = pytest.mark.mount

# A path nothing else in the suite publishes to, so the revision the mount
# caches stays put and "was this a fetch or a cache hit" stays answerable.
READ = "public/archive/old-post.md"
READ_PATH = "root.public.archive.old-post"


@pytest.fixture(scope="session")
def audited(mount_factory, tmp_path_factory):
    """A mount that audits, with its own spool directory.

    Its own directory because the queue is a file on disk that outlives a
    process: sharing the developer's real one would make the suite's results
    depend on what they had been reading.

    A short shipping interval, because the default is thirty seconds and the
    thing under test is what arrives, not how patiently it waits.
    """
    spool = tmp_path_factory.mktemp("audit-spool")
    m = mount_factory("--audit", "--audit-dir", str(spool),
                      "--audit-interval", "1")
    m.spool = spool
    return m


def events(stack, path: str = READ_PATH) -> list[dict]:
    raw = stack.psql(
        "select coalesce(jsonb_agg(to_jsonb(e) order by e.occurred_at), '[]') "
        f"from wiki.access_event e where e.path = '{path}'")
    return json.loads(raw)


def queued(mount) -> list[dict]:
    """Whatever is still spooled locally, live file and in-flight file both."""
    out = []
    for name in ("audit.jsonl", "audit.sending.jsonl"):
        f = mount.spool / name
        if f.exists():
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return out


def uncached_read(mount, rel: str = READ) -> str:
    """Read a file in a way that cannot be served from the mount's own cache.

    The content cache is keyed on the published revision, so the *first* read
    after a mount starts is a fetch and every one after it is not. Tests that
    want a fetch have to say so, and the only honest way to say it from out
    here is to read something this mount has not read before.
    """
    return (mount.path / rel).read_text()


# ---------------------------------------------------------------------------
# The ordinary path: no queue involved
# ---------------------------------------------------------------------------

def test_an_open_is_recorded_by_the_fetch_that_serves_it(audited, clean):
    """One round trip, and nothing on disk to lose if the mount dies."""
    uncached_read(audited)
    rows = wait_for(lambda: events(clean), what="the read to be recorded")
    assert len(rows) == 1
    assert rows[0]["action"] == "open"
    assert not queued(audited), "the fetch carried it; nothing should be spooled"


def test_the_event_names_the_program_that_opened_the_file(audited, clean):
    """`cat` and an editor are different questions about the same document, and
    the difference is the only thing an access trail can offer that a revision
    history cannot."""
    subprocess.run(["cat", str(audited / "public/welcome.md")],
                   capture_output=True, check=True)
    rows = wait_for(lambda: events(clean, "root.public.welcome"),
                    what="cat's read to be recorded")
    assert rows[0]["process"]["comm"] == "cat"


def test_the_command_line_is_truncated_to_the_program(audited, clean):
    """Command lines routinely carry passwords and API keys that have nothing
    to do with the wiki, and shipping them would put someone else's secrets in
    this database and then in its backups. argv[0] is what identifies the
    program, so argv[0] is what travels — and what was dropped is counted
    rather than silently omitted, so a bare command is distinguishable from a
    truncated one."""
    subprocess.run(["cat", str(audited / "public/guide/mounting.md")],
                   capture_output=True, check=True)
    rows = wait_for(lambda: events(clean, "root.public.guide.mounting"),
                    what="the read to be recorded")
    process = rows[0]["process"]
    assert len(process["cmdline"]) == 1
    assert process["cmdline"][0].endswith("cat")
    assert process["argv_elided"] == 1, "the path we passed cat is gone"


def test_the_identity_is_the_tokens_whatever_the_payload_says(audited, clean):
    uncached_read(audited)
    rows = wait_for(lambda: events(clean), what="the read to be recorded")
    assert rows[0]["principal_id"] == clean.who("bob")


# ---------------------------------------------------------------------------
# The queue, and why it is still there
# ---------------------------------------------------------------------------

def test_a_cached_re_read_still_reaches_the_trail(audited, clean):
    """The second open of a revision is served from the mount's own cache, so
    the server never sees a request for it. Without the spool the read would
    simply not be in the trail, and a trail with a hole in it that depends on
    what you read earlier is worse than no trail.
    """
    uncached_read(audited)
    first = wait_for(lambda: events(clean), what="the first read")
    assert len(first) == 1

    (audited / READ).read_text()
    both = wait_for(lambda: len(events(clean)) == 2,
                    what="the cached re-read to be shipped from the queue")
    assert both


def test_the_queue_drains_and_stays_drained(audited, clean):
    (audited / READ).read_text()
    wait_for(lambda: events(clean), what="the read to be recorded")
    assert wait_for(lambda: not queued(audited),
                    what="the spool to empty")


def test_the_same_event_arriving_twice_is_one_row(audited, clean):
    """The client queues an event *before* offering it to a fetch, so the same
    id routinely arrives by both routes. The primary key is the client's, which
    is what makes that a no-op rather than a duplicate."""
    uncached_read(audited)
    wait_for(lambda: events(clean), what="the read to be recorded")
    rows = events(clean)
    assert len({r["event_id"] for r in rows}) == len(rows)


# ---------------------------------------------------------------------------
# Reads that never leave the machine
# ---------------------------------------------------------------------------

def test_listing_a_directory_is_not_an_access(audited, clean):
    """`ls` is served from the manifest the mount already holds. Recording it
    would fill the trail with the poller's own footsteps and drown the reads a
    person actually made."""
    before = clean.count("select count(*) from wiki.access_event")
    for _ in range(3):
        os.listdir(audited.path / "public")
    assert clean.count("select count(*) from wiki.access_event") == before


def test_a_local_only_file_is_nobodys_business(audited, clean):
    """A scratch file never leaves this process, so there is nothing for the
    server to have an opinion about."""
    before = clean.count("select count(*) from wiki.access_event")
    scratch = audited / "public/.probe.swp"
    scratch.write_text("editor droppings\n")
    scratch.read_text()
    assert clean.count("select count(*) from wiki.access_event") == before
    scratch.unlink()


# ---------------------------------------------------------------------------
# Refusals are the interesting half
# ---------------------------------------------------------------------------

def test_auditing_needs_a_token(stack, tmp_path):
    """Events are filed against a principal, and anonymous is not one. Refusing
    at startup beats mounting and building a queue that can never ship."""
    base = tmp_path / "mnt"
    base.mkdir()
    env = stack.env("bob")
    env.pop("FSWIKI_TOKEN")
    out = subprocess.run(["fswiki-mount", str(base), "--audit"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode != 0
    assert "token" in (out.stdout + out.stderr).lower()


def test_argv_shipping_says_so_out_loud(stack, tmp_path, mount_factory):
    """It sends other people's secrets to the server and into its backups. That
    is a legitimate fleet policy and an illegitimate surprise, so the mount says
    it every time rather than only in the help text."""
    spool = tmp_path / "spool"
    m = mount_factory("--audit", "--audit-dir", str(spool), "--audit-argv")
    said = wait_for(lambda: "audit-argv" in m.log.read_text() or None,
                    what="the warning to be printed")
    assert said
    assert "secrets" in m.log.read_text()
