"""The audit queue as a queue: the cap, the batching, the crash recovery.

`test_mount_audit.py` drives this through a real filesystem, which is the right
way to ask whether a read reaches the trail. It is the wrong way to ask what
happens at eight megabytes, or to a torn last line from a killed process —
those need either a very long test or a lie about how much was written.

So this drives `AuditLog` directly, against a stub client. No mount, no
PostgREST, no /dev/fuse, and it runs in a build sandbox at unit-test speed.

The property underneath all of it: **a queue that drops silently is worse than
no queue**, because its gaps are indistinguishable from nothing having
happened. Everything here is ultimately about that.
"""

from __future__ import annotations

import json

import anyio
import pytest

from fswiki_fuse.audit import AuditLog, QUEUE_CAP_BYTES, _batched

pytestmark = pytest.mark.anyio


class Stub:
    """A client that records what it was asked to ship, and can refuse."""

    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[dict]] = []
        self.fail = fail

    async def record_opens(self, batch: list[dict]) -> int:
        if self.fail:
            raise ConnectionError("no server")
        self.batches.append(batch)
        return len(batch)

    @property
    def shipped(self) -> list[dict]:
        return [e for b in self.batches for e in b]


def make(tmp_path, *, cap: int = QUEUE_CAP_BYTES, fail: bool = False):
    stub = Stub(fail=fail)
    return AuditLog(stub, tmp_path / "spool", interval=0.01, cap_bytes=cap), stub


def event(log: AuditLog, path: str, **extra) -> dict:
    return log.event(document_id=None, path=path, **extra)


# ---------------------------------------------------------------------------
# Ordinary shipping
# ---------------------------------------------------------------------------

async def test_what_is_queued_is_what_is_shipped(tmp_path):
    log, stub = make(tmp_path)
    for i in range(5):
        log.queue(event(log, f"root.public.p{i}"))
    await log.flush()
    assert [e["path"] for e in stub.shipped] == [f"root.public.p{i}" for i in range(5)]


async def test_flushing_an_empty_queue_is_not_a_request(tmp_path):
    """The shipper runs on a timer, so most flushes have nothing to do. One
    request per interval per idle mount would be a poll nobody asked for."""
    log, stub = make(tmp_path)
    await log.flush()
    assert stub.batches == []


async def test_a_drained_queue_leaves_no_files_behind(tmp_path):
    log, stub = make(tmp_path)
    log.queue(event(log, "root.public.welcome"))
    await log.flush()
    assert not list((tmp_path / "spool").glob("*.jsonl"))


async def test_order_is_kept_across_flushes(tmp_path):
    """The oldest events are the ones most likely to be about something that
    has since gone wrong, so a queue that reorders under pressure is a queue
    that loses exactly the wrong end."""
    log, stub = make(tmp_path)
    log.queue(event(log, "root.public.first"))
    await log.flush()
    log.queue(event(log, "root.public.second"))
    await log.flush()
    assert [e["path"] for e in stub.shipped] == ["root.public.first",
                                                 "root.public.second"]


# ---------------------------------------------------------------------------
# A failed attempt
# ---------------------------------------------------------------------------

async def test_a_failed_flush_keeps_the_batch(tmp_path):
    log, stub = make(tmp_path, fail=True)
    log.queue(event(log, "root.public.welcome"))
    with pytest.raises(ConnectionError):
        await log.flush()

    spooled = _spooled(tmp_path)
    assert [e["path"] for e in spooled] == ["root.public.welcome"]


async def test_the_leftover_batch_goes_first_next_time(tmp_path):
    """Otherwise a mount that keeps failing and keeps reading would starve its
    oldest events forever, and they are the ones worth having."""
    log, stub = make(tmp_path, fail=True)
    log.queue(event(log, "root.public.older"))
    with pytest.raises(ConnectionError):
        await log.flush()

    log.queue(event(log, "root.public.newer"))
    stub.fail = False
    await log.flush()
    await log.flush()
    assert [e["path"] for e in stub.shipped] == ["root.public.older",
                                                 "root.public.newer"]


async def test_a_torn_last_line_does_not_block_the_batch(tmp_path):
    """A process killed mid-write leaves half a line. Refusing to ship the
    batch it is part of would mean one interrupted save costs every event
    behind it, permanently."""
    log, stub = make(tmp_path)
    log.queue(event(log, "root.public.welcome"))
    with open(tmp_path / "spool" / "audit.jsonl", "a") as fh:
        fh.write('{"event_id": "half a line')

    await log.flush()
    assert [e["path"] for e in stub.shipped] == ["root.public.welcome"]


async def test_a_queue_of_nothing_but_rubbish_is_thrown_away(tmp_path):
    """Not retried forever. A file that can never be parsed is not a queue."""
    log, stub = make(tmp_path)
    (tmp_path / "spool").mkdir(parents=True)
    (tmp_path / "spool" / "audit.jsonl").write_text("not json at all\n")

    await log.flush()
    assert stub.batches == []
    assert not list((tmp_path / "spool").glob("*.jsonl"))


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------

async def test_the_cap_stops_the_queue_growing(tmp_path):
    """A laptop offline for a fortnight must not fill the disk with a record
    nobody has asked for."""
    log, stub = make(tmp_path, cap=1024)
    for i in range(500):
        log.queue(event(log, f"root.public.p{i}"))

    size = (tmp_path / "spool" / "audit.jsonl").stat().st_size
    assert size <= 1024, size


async def test_dropping_is_reported_and_counted(tmp_path, caplog):
    """A gap nobody can see is indistinguishable from nothing having happened,
    which is the one thing an audit trail may never be."""
    log, stub = make(tmp_path, cap=512)
    for i in range(200):
        log.queue(event(log, f"root.public.p{i}"))
    assert log._dropped > 0
    assert any("audit queue is full" in r.message for r in caplog.records)


async def test_room_freed_by_a_flush_is_room_again(tmp_path):
    """The cap is on what is *unshipped*. A mount that hit it once and could
    never queue again would stop auditing for the rest of the session."""
    log, stub = make(tmp_path, cap=1024)
    for i in range(500):
        log.queue(event(log, f"root.public.p{i}"))
    await log.flush()

    log.queue(event(log, "root.public.after"))
    await log.flush()
    assert stub.shipped[-1]["path"] == "root.public.after"


async def test_a_queue_left_by_a_previous_session_counts_against_the_cap(tmp_path):
    """Seeded from disk rather than from zero. Starting the count at zero would
    let every restart buy another cap's worth, which on a laptop that mounts
    twice a day is not a cap at all."""
    log, _ = make(tmp_path, cap=2048)
    for i in range(200):
        log.queue(event(log, f"root.public.p{i}"))
    on_disk = (tmp_path / "spool" / "audit.jsonl").stat().st_size

    again, stub = make(tmp_path, cap=2048)
    assert again._size == on_disk

    for i in range(200):
        again.queue(event(again, f"root.public.q{i}"))
    assert (tmp_path / "spool" / "audit.jsonl").stat().st_size <= 2048


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def test_a_batch_is_split_by_bytes_not_by_count():
    """One flush is one request. Big enough to be worth a round trip, small
    enough that a failure does not re-send a megabyte."""
    events = [{"event_id": str(i), "path": "x" * 100} for i in range(100)]
    batches = _batched(events, 1024)
    assert len(batches) > 1
    for batch in batches:
        assert sum(len(json.dumps(e, separators=(",", ":"))) for e in batch) <= 1024 \
            or len(batch) == 1


def test_nothing_is_lost_or_duplicated_by_batching():
    events = [{"event_id": str(i)} for i in range(37)]
    flat = [e for b in _batched(events, 40) for e in b]
    assert flat == events


def test_an_event_larger_than_the_limit_still_goes():
    """Dropping it would mean a document with a long enough path could never be
    audited at all — a hole an attacker gets to choose."""
    events = [{"event_id": "1", "path": "x" * 5000}]
    assert _batched(events, 100) == [events]


def test_no_batches_for_no_events():
    assert _batched([], 1024) == []


def _spooled(tmp_path) -> list[dict]:
    out = []
    for name in ("audit.jsonl", "audit.sending.jsonl"):
        f = tmp_path / "spool" / name
        if f.exists():
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return out


# ---------------------------------------------------------------------------
# Minting and queueing in one step
# ---------------------------------------------------------------------------

async def test_recording_mints_and_queues_together(tmp_path):
    """The common case for everything but a read. A read mints the event and
    offers it to the fetch first, because a request that carries the event is
    durable in a way a local queue is not; everything else has no fetch to
    hang it on."""
    log, stub = make(tmp_path)
    minted = log.record(document_id=None, path="root.a", action="denied")
    await log.flush()
    assert stub.shipped == [minted]
    assert minted["action"] == "denied"
    assert minted["event_id"] and minted["occurred_at"]


async def test_every_event_gets_its_own_identity(tmp_path):
    """record_opens is idempotent on the id, which is what lets a client that
    never saw the response resend the batch. Two events sharing an id would
    make the second one vanish."""
    log, stub = make(tmp_path)
    for _ in range(5):
        log.record(document_id=None, path="root.a")
    await log.flush()
    assert len({e["event_id"] for e in stub.shipped}) == 5


# ---------------------------------------------------------------------------
# When the disk says no
# ---------------------------------------------------------------------------

async def test_a_disk_that_refuses_the_write_does_not_take_the_mount_down(
        tmp_path, caplog):
    """This runs inside open(). A full or read-only disk is an ordinary thing
    for a laptop to be, and an exception out of a FUSE handler does not fail
    the syscall — it comes out of pyfuse3.main() and unmounts everything."""
    # A file where the spool directory should be, rather than a chmod: this
    # has to fail the same way when the suite runs as root, and root ignores
    # permission bits.
    (tmp_path / "spool").write_text("not a directory")

    log, stub = make(tmp_path)
    log.record(document_id=None, path="root.a")
    assert "could not queue" in caplog.text

    await log.flush()
    assert stub.shipped == []


async def test_a_blank_line_in_the_queue_is_skipped_silently(tmp_path):
    """Distinct from a torn line: a stray newline is not a lost event and
    counting it as malformed would report damage that did not happen."""
    log, stub = make(tmp_path)
    log.record(document_id=None, path="root.a")
    live = tmp_path / "spool" / "audit.jsonl"
    live.write_text(live.read_text() + "\n   \n")
    await log.flush()
    assert len(stub.shipped) == 1


# ---------------------------------------------------------------------------
# The shipper task
# ---------------------------------------------------------------------------

async def test_the_shipper_drains_on_its_own(tmp_path):
    """`run()` is what the mount actually starts; every test above calls
    flush() by hand and so never exercises the loop around it."""
    log, stub = make(tmp_path)
    log.record(document_id=None, path="root.a")
    async with anyio.create_task_group() as tg:
        tg.start_soon(log.run)
        with anyio.fail_after(5):
            while not stub.shipped:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()
    assert len(stub.shipped) == 1


async def test_the_shipper_survives_a_server_that_is_refusing(tmp_path, caplog):
    """The one property that matters about the loop. A shipper that dies on a
    failed flush stops shipping *forever*, and nothing says so: the mount
    keeps working, the queue keeps filling, and the trail quietly stops. It
    must log and come back round."""
    log, stub = make(tmp_path, fail=True)
    log.record(document_id=None, path="root.a")
    async with anyio.create_task_group() as tg:
        tg.start_soon(log.run)
        with anyio.fail_after(5):
            while "audit flush failed" not in caplog.text:
                await anyio.sleep(0.01)
        # Still alive: the server comes back, and the queue it kept goes out.
        stub.fail = False
        with anyio.fail_after(5):
            while not stub.shipped:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()
    assert [e["path"] for e in stub.shipped] == ["root.a"]
