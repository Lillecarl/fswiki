"""The client end of the audit trail: capture now, ship later.

Recording must never make a file operation slower or more likely to fail, and
shipping must never lose an event because the laptop was on a train. Those two
pull in opposite directions, so the queue is split:

* `record()` appends one JSON line and returns. No network, no fsync, no lock.
* a background task rotates the file and POSTs it in one batch.

Rotation rather than truncation is what makes that race-free. The live file is
renamed aside before anything is read from it, so events arriving mid-flight
land in a fresh file and cannot be dropped by a truncate that was decided before
they existed. A batch that fails to ship stays on disk under its rotated name
and is retried ahead of anything newer.

Delivery is at-least-once: a batch whose response was lost gets sent again, and
the server dedupes on the client-generated event id. Losing the acknowledgement
is common; losing the event is not acceptable.

See docs/audit-trail.md for what these records are worth. Briefly: the process
fields are the user's own machine describing itself, and two of them are
trivially forgeable. Treat them as a hint, never as proof.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anyio

from fswiki_core.client import Client, PostgrestError

log = logging.getLogger(__name__)

# One flush is one request, so the batch wants to be big enough to be worth a
# round trip and small enough that a failure does not re-send a megabyte.
BATCH_BYTES = 256 * 1024
# How much unshipped history is allowed to accumulate before new events are
# dropped. A laptop offline for a fortnight should not fill the disk with a
# record nobody has asked for.
QUEUE_CAP_BYTES = 8 * 1024 * 1024


class AuditLog:
    """An append-only local queue that drains to `wiki.record_opens()`."""

    def __init__(
        self,
        client: Client,
        directory: str | os.PathLike[str],
        *,
        interval: float = 30.0,
        cap_bytes: int = QUEUE_CAP_BYTES,
        full_cmdline: bool = False,
    ) -> None:
        # Whether the caller's whole argv is shipped or only argv[0]. Off,
        # because command lines carry other people's secrets and none of them
        # are the wiki's business. See procinfo.describe().
        self.full_cmdline = full_cmdline
        self._client = client
        self._dir = Path(directory)
        self._live = self._dir / "audit.jsonl"
        self._sending = self._dir / "audit.sending.jsonl"
        self._interval = interval
        self._cap = cap_bytes

        self._dropped = 0
        # Seeded from disk, not from zero. A queue that survived the last
        # session still occupies the cap it was capped by; starting the count
        # at zero would let every restart buy another 8 MB, which on a laptop
        # that mounts twice a day is not a cap at all.
        self._size = _size_of(self._live) + _size_of(self._sending)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    @staticmethod
    def event(
        *,
        document_id: str | None,
        path: str | None,
        action: str = "open",
        open_flags: int | None = None,
        process: dict | None = None,
    ) -> dict:
        """Mint one event. Does not queue it — see `queue()`.

        Separate from queueing because an event has two ways to reach the
        server and they are not alternatives. A content fetch can carry it on
        the request that serves the bytes, which is durable and needs no queue
        at all; but a refused open never fetches anything, a body already in a
        draft is never fetched either, and a laptop on a train cannot fetch. So
        the event is minted here, offered to the fetch, and queued only if the
        fetch did not take it.

        The id is generated here and used by both routes, so if they both
        happen — a retry, a crash between the two — the server's `on conflict`
        collapses them into one row.
        """
        return {
            "event_id": str(uuid.uuid4()),
            "document_id": document_id,
            "path": path,
            "action": action,
            "open_flags": open_flags,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "process": process,
        }

    def record(self, **fields) -> dict:
        """Mint an event and queue it. The common case for anything but a read."""
        event = self.event(**fields)
        self.queue(event)
        return event

    def queue(self, event: dict) -> None:
        """Spool one event locally. Never raises, never blocks on anything remote."""
        line = json.dumps(event, separators=(",", ":")) + "\n"

        if self._size + len(line) > self._cap:
            # Dropping is reported, not silent: a gap that nobody can see is
            # indistinguishable from nothing having happened.
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                log.warning("audit queue is full (%d bytes); dropped %d events",
                            self._size, self._dropped)
            return

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # 0600 from the start. The file names every document this user has
            # opened, which is nobody else's business on a shared machine.
            fd = os.open(self._live, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            self._size += len(line)
        except OSError as exc:
            # A full or read-only disk must not take the mount down.
            log.warning("could not queue an audit event: %s", exc)

    # ------------------------------------------------------------------
    # Shipping
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drain the queue forever. Intended for a nursery."""
        while True:
            # Flush first, then sleep. A queue left behind by a session that
            # crashed or lost the network should not wait out an interval it
            # has already waited out once.
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001 - a shipper must not die
                log.warning("audit flush failed, will retry: %s", exc)
            await anyio.sleep(self._interval)

    async def flush(self) -> None:
        """Send whatever is queued. Safe to call at any time."""
        # A batch left over from a failed attempt goes first, so events keep
        # their order and a persistent failure does not starve the oldest.
        if not self._sending.exists():
            if not self._live.exists() or self._live.stat().st_size == 0:
                return
            # The rename is what makes this race-free, and it moves bytes from
            # one side of the cap to the other rather than freeing them.
            self._live.rename(self._sending)

        raw = self._sending.read_text(encoding="utf-8", errors="replace")
        events, malformed = [], 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                # A torn last line from a killed process. Skip it rather than
                # refusing to ship the batch it is part of.
                malformed += 1
        if malformed:
            log.warning("discarded %d unreadable audit lines", malformed)

        if not events:
            self._sending.unlink(missing_ok=True)
            self._recount()
            return

        for batch in _batched(events, BATCH_BYTES):
            accepted = await self._client.record_opens(batch)
            log.debug("shipped %d audit events, %s new", len(batch), accepted)

        self._sending.unlink(missing_ok=True)
        self._recount()

        if self._dropped:
            log.info("audit queue drained; %d events had been dropped while full",
                     self._dropped)
            self._dropped = 0


    def _recount(self) -> None:
        """Re-derive the queued size from disk after files come and go."""
        self._size = _size_of(self._live) + _size_of(self._sending)


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _batched(events: list[dict], limit: int) -> list[list[dict]]:
    """Split into batches no bigger than `limit` bytes of JSON."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for event in events:
        cost = len(json.dumps(event, separators=(",", ":")))
        if current and size + cost > limit:
            batches.append(current)
            current, size = [], 0
        current.append(event)
        size += cost
    if current:
        batches.append(current)
    return batches
