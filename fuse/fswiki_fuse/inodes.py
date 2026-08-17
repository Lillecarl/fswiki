"""Inode numbers for things that are identified by uuid.

FUSE speaks 64-bit integers; the wiki speaks uuids and, for drafts and scratch
files, synthetic string keys. This is the map between them, plus the lookup
counting the kernel protocol requires.

Two properties matter and both are easy to get wrong:

* **Stability.** The same key must keep the same inode for as long as the kernel
  remembers it, across manifest refreshes. Hand out a new inode for a file the
  kernel still has open and it becomes a different file underneath the
  application.
* **Lifetime.** Every entry handed back from `lookup`, `create` or `readdir`
  increments a count the kernel later gives back through `forget`. Drop a
  mapping early and an open file handle dangles; never drop one and a long-lived
  mount leaks an entry per file ever listed.
"""

from __future__ import annotations

from collections import Counter

import pyfuse3

ROOT_INODE = pyfuse3.ROOT_INODE


class InodeTable:
    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._by_inode: dict[int, str] = {}
        self._lookups: Counter[int] = Counter()
        self._next = ROOT_INODE + 1

    def pin_root(self, key: str) -> None:
        """Bind a key to inode 1, which the kernel assumes is the mount root."""
        existing = self._by_inode.get(ROOT_INODE)
        if existing == key:
            return
        if existing is not None:
            del self._by_key[existing]
        self._by_key[key] = ROOT_INODE
        self._by_inode[ROOT_INODE] = key

    def inode_for(self, key: str) -> int:
        inode = self._by_key.get(key)
        if inode is None:
            inode = self._next
            self._next += 1
            self._by_key[key] = inode
            self._by_inode[inode] = key
        return inode

    def key_for(self, inode: int) -> str | None:
        return self._by_inode.get(inode)

    def remember(self, inode: int, count: int = 1) -> None:
        """Record that the kernel is now holding a reference."""
        if inode != ROOT_INODE:
            self._lookups[inode] += count

    def forget(self, inode: int, count: int) -> None:
        if inode == ROOT_INODE:
            return
        remaining = self._lookups[inode] - count
        if remaining > 0:
            self._lookups[inode] = remaining
            return
        del self._lookups[inode]
        key = self._by_inode.pop(inode, None)
        if key is not None and self._by_key.get(key) == inode:
            del self._by_key[key]

    def rekey(self, old: str, new: str) -> None:
        """Move an existing inode onto a new key.

        Needed when a file stops being one kind of thing and becomes another
        without the kernel being told: a scratch file renamed onto a real slug
        keeps its inode and gains a draft key.
        """
        inode = self._by_key.pop(old, None)
        if inode is None:
            return
        self._by_key[new] = inode
        self._by_inode[inode] = new

    def __len__(self) -> int:
        return len(self._by_inode)
