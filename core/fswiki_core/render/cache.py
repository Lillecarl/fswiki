"""Rendered bodies, kept because nothing can make them wrong.

The hard part of caching is knowing when to throw something away, and here it
does not exist. A revision's content never changes -- that is what
`document_version` means -- so `(document_id, version, renderer)` names one
byte string forever. There is nothing to invalidate. There is only something to
evict.

Two properties make the key correct, and both are worth stating because losing
either one turns this into a security bug rather than a slow page.

**It is identity-independent.** What goes in here is the *neutral* body, with
wiki links still under the reserved prefix. Resolving them against what a
particular reader may see happens afterwards, per request, in
`render.links.resolve`. So the bytes are the same for everyone entitled to
them, and one reader's link graph can never reach another's. Cache the composed
page instead and that stops being true immediately.

**It is a published revision, never a draft.** A draft has mutable content and
no version, so it has no key here at all. `Pages` does not offer one, rather
than offering one that is wrong.

The renderer is in the key because the pipeline decides the bytes as much as
the content does. See `render._renderer_id`: engine, configuration, and the
passes on either side. Leave it out and an upgrade quietly serves what the old
code produced.

Bounded on **bytes** rather than on entries, because entries here are pages and
a wiki's pages differ in size by three orders of magnitude. A bound on the
count of them is not a bound on the memory they hold.

Per process, on purpose. Two servers render a page once each. Sharing would
mean a network hop against a 2 us lookup, which is the wrong trade by four
orders of magnitude.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

#: 32 MiB of HTML. A few thousand typical pages, and small beside the process.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Key:
    """What names one byte string forever.

    `version` and `document_id` come straight from the row that was read;
    `renderer` is `Rendered.renderer`, which identifies the whole pipeline
    rather than only the engine.
    """

    document_id: str
    version: int
    renderer: str


class Cache:
    """A bounded LRU of neutral bodies. Safe to share between threads."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        # value is (html, its size in bytes), so eviction does not re-encode
        # a string it is about to drop.
        self._entries: OrderedDict[Key, tuple[str, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        #: Bodies too large to store at all. Counted rather than logged: it is
        #: a sizing signal, and one page can produce it on every request.
        self.oversized = 0

    def get(self, key: Key) -> str | None:
        """The stored body, or None. Never raises, whatever the key is."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[0]

    def put(self, key: Key, html: str) -> None:
        """Store a body, evicting the least recently used until it fits."""
        size = len(html.encode())
        with self._lock:
            if size > self.max_bytes:
                # Storing it would evict everything and then itself, which is
                # a cache that holds one page and misses on all of them.
                self.oversized += 1
                return
            if key in self._entries:
                self._bytes -= self._entries.pop(key)[1]
            self._entries[key] = (html, size)
            self._bytes += size
            while self._bytes > self.max_bytes:
                self._bytes -= self._entries.popitem(last=False)[1][1]
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    @property
    def nbytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def stats(self) -> dict:
        """Counters, for a log line at shutdown or an operator asking."""
        return {"entries": len(self._entries), "bytes": self._bytes,
                "max_bytes": self.max_bytes, "hits": self.hits,
                "misses": self.misses, "evictions": self.evictions,
                "oversized": self.oversized}
