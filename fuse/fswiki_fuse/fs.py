"""The pyfuse3 operations.

Shape of the thing: one manifest request builds the whole visible tree, and
document bodies are fetched lazily per open. Writes never touch published
history — they land in `wiki.draft`, which is exactly the working copy the
server already models. Publishing is `wiki.push()`, and that belongs to the CLI.

Two ideas carry most of the weight.

**Scratch files.** A name the server could never accept as a slug — `.foo.swp`,
`bar.md~`, `#notes#` — becomes a local-only file held in memory and never sent
anywhere. Without this the mount is unusable with a normal editor, because vim,
emacs and VS Code all save by writing a sibling temp file and renaming it over
the target. With it, that dance works: the rename is what turns buffered bytes
into a draft.

**Everything is judged server-side.** The capability sets in the manifest decide
which mode bits to show, and nothing more. A write the ACL forbids fails when
the draft is posted, not because this file guessed.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import anyio
import pyfuse3

from fswiki_core import naming
# Both, everywhere, and never one without the other. An exception out of a FUSE
# handler does not fail the syscall — it comes out of pyfuse3.main() and takes
# the whole filesystem down, leaving a mountpoint that hangs every `ls` until
# someone unmounts it by hand. PostgrestError is "the server said no";
# Unreachable is "there was no server", which is httpx.TransportError and
# therefore not an OSError either. A handler that catches only the first works
# perfectly until the wifi drops.
from fswiki_core.client import Client, PostgrestError, Unreachable
from . import procinfo
from .audit import AuditLog
from .inodes import ROOT_INODE, InodeTable
from .model import Node, Tree, build, draft_body

log = logging.getLogger(__name__)

XATTR_PREFIX = "user.fswiki."


def _public_url(value: str) -> str:
    """A connection address suitable for a world-readable marker."""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1],
                       parsed.path, parsed.query, parsed.fragment))


@dataclass
class Scratch:
    """A local-only file or directory. Never leaves this process."""

    key: str
    parent_key: str
    name: str
    is_dir: bool = False
    data: bytearray = field(default_factory=bytearray)
    mtime: float = 0.0
    read_only: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Handle:
    """One open file."""

    key: str
    data: bytearray
    writable: bool
    dirty: bool = False


@dataclass
class _Carrier:
    """An audit event offered to a content fetch, and whether it took it.

    An event has two ways to reach the server. The fetch can carry it, which
    costs nothing and commits it alongside the bytes it describes; failing
    that, the local queue ships it later. Exactly one of them should, so this
    passes the event down to whoever might carry it and reports back whether
    anyone did. The alternative — threading a second return value through
    `_load` and everything it can raise past — loses the answer on precisely
    the paths where it matters most.
    """

    event: dict | None
    taken: bool = False


def _errno_for(exc: PostgrestError | Unreachable) -> int:
    """EACCES when the server refused us, EIO for anything else.

    `Unreachable` has no `status` — there was no response for one to be on —
    and reaching for it regardless is how a network blink turned into an
    AttributeError raised out of a FUSE handler. That does not fail the write;
    it ends the filesystem. Asking what the exception *is* costs one isinstance
    and cannot go wrong that way.
    """
    return (errno.EACCES
            if isinstance(exc, PostgrestError) and exc.status in (401, 403)
            else errno.EIO)


class FswikiFs(pyfuse3.Operations):
    supports_dot_lookup = True
    enable_writeback_cache = False

    def __init__(
        self,
        client: Client,
        principal_id: str | None,
        *,
        ttl: float = 5.0,
        poll: float | None = None,
        read_only: bool = False,
        show_drafts: bool = True,
        audit: "AuditLog | None" = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._principal_id = principal_id
        # Off unless asked for. Identifying the caller costs microseconds, but
        # it is still a record of what someone did on their own machine, and
        # that should be a decision rather than a default.
        self._audit = audit
        # What the kernel is told it may cache for.
        self._ttl = ttl
        # How often we are willing to ask the server anything at all. Defaults
        # to the kernel TTL, but can be shorter: a poll is a few bytes, so
        # checking more often than the kernel asks is nearly free.
        self._poll = ttl if poll is None else poll
        self._read_only = read_only or principal_id is None
        # Not the same question as read_only, though it used to ride on it. An
        # impersonated mount is read-only and *should* show the subject's
        # drafts, because a draft is part of what that person sees when they
        # look at their own wiki -- and "I can't see X" is quite often about a
        # file they have not pushed. Anonymous still gets none: there is nobody
        # for a draft to belong to.
        self._show_drafts = show_drafts and principal_id is not None

        self._inodes = InodeTable()
        self._tree: Tree | None = None
        self._fetched_at = float("-inf")
        self._checked_at = float("-inf")
        self._token: str | None = None
        self._token_supported = True
        self._refresh_lock = anyio.Lock()

        # document key -> the revision we last showed this user. One integer per
        # document ever opened, same bound as the content cache below.
        self._checked_out: dict[str, int] = {}

        self._scratch: dict[str, Scratch] = {}
        self._scratch_seq = 0

        self._handles: dict[int, Handle] = {}
        self._handle_seq = 0

        # key -> (etag, bytes). Bounded by the number of files ever opened in
        # one mount, which for a wiki is fine; revisit if attachments arrive.
        self._content: dict[str, tuple[str, bytes]] = {}

        self._uid = os.getuid()
        self._gid = os.getgid()
        metadata = {
            "format": "fswiki-mount",
            "version": 1,
            "url": _public_url(client.base_url),
        }
        self._metadata = Scratch(
            key="virtual:.fswiki",
            parent_key="",
            name=".fswiki",
            data=bytearray((json.dumps(metadata, sort_keys=True) + "\n").encode()),
            read_only=True,
        )

    @property
    def read_only(self) -> bool:
        """Whether this mount refuses writes, for whatever reason.

        Read by the caller to pick the kernel's mount options, so it has to be
        the settled answer rather than the flag that was passed in: no token
        and impersonation both arrive here as well.
        """
        return self._read_only

    # ------------------------------------------------------------------
    # Tree maintenance
    # ------------------------------------------------------------------

    async def refresh(self, *, force: bool = False) -> Tree:
        """Bring the tree up to date, as cheaply as the server allows.

        Two intervals, and keeping them apart is the whole point:

        * `ttl` is what the *kernel* is told, so most operations never reach us
          at all;
        * `poll` is how often we ask the server anything, and what we ask is
          `change_token()` — a few bytes — not the six-kilobyte manifest.

        So the steady state for an idle mount is one tiny request every `poll`
        seconds, and a manifest fetch only when someone actually wrote
        something.
        """
        if not force and self._tree is not None:
            if anyio.current_time() - self._checked_at < self._poll:
                return self._tree

        async with self._refresh_lock:
            # Re-check inside the lock: a burst of concurrent lookups should
            # cost one request, not one each.
            if not force and self._tree is not None:
                if anyio.current_time() - self._checked_at < self._poll:
                    return self._tree

            # Always sampled *before* the manifest, never after. A write landing
            # mid-fetch then leaves the stored token stale relative to the tree,
            # so the next poll refreshes again — wasteful but correct. Sampling
            # afterwards would record a token that already covers the write and
            # silently lose it.
            token: str | None = None
            if self._token_supported:
                try:
                    token = await self._client.change_token()
                    if token is None:
                        log.debug("server has no change_token(); refetching every poll")
                        self._token_supported = False
                except (PostgrestError, Unreachable) as exc:
                    log.debug("change_token failed, refetching: %s", exc)

            if (
                not force
                and self._tree is not None
                and token is not None
                and token == self._token
            ):
                self._checked_at = anyio.current_time()
                return self._tree

            manifest = await self._client.manifest()
            drafts = await self._client.drafts() if self._show_drafts else []
            tree = build(manifest, drafts)

            self._inodes.pin_root(tree.root_key)
            self._tree = tree
            self._token = token
            now = anyio.current_time()
            self._checked_at = now
            self._fetched_at = now
            log.debug("manifest: %d documents, %d drafts", len(manifest), len(drafts))
            return tree

    async def _current(self) -> Tree:
        try:
            return await self.refresh()
        except (PostgrestError, Unreachable) as exc:
            log.error("manifest refresh failed: %s", exc)
            if self._tree is None:
                raise pyfuse3.FUSEError(errno.EIO) from exc
            # Serve the last good tree rather than blanking the mount because
            # the network blinked.
            return self._tree

    def _resolve(self, inode: int) -> Node | Scratch | None:
        key = self._inodes.key_for(inode)
        if key is None:
            return None
        if key == self._metadata.key:
            return self._metadata
        if key in self._scratch:
            return self._scratch[key]
        return self._tree.get(key) if self._tree else None

    def _parent_key(self, entry: Node | Scratch) -> str | None:
        if isinstance(entry, Scratch):
            return entry.parent_key
        parent_path = naming.ltree_parent(entry.path)
        if parent_path is None or self._tree is None:
            return None
        return self._tree.by_path.get(parent_path)

    def _dir_entries(self, key: str) -> dict[str, Node | Scratch]:
        """Everything visible inside a directory: server nodes then scratch."""
        entries: dict[str, Node | Scratch] = {}
        if self._tree is not None:
            for name, child_key in self._tree.children.get(key, {}).items():
                node = self._tree.get(child_key)
                if node is not None:
                    entries[name] = node
        for scratch in self._scratch.values():
            if scratch.parent_key == key:
                entries[scratch.name] = scratch
        if self._tree is not None and key == self._tree.root_key:
            self._metadata.parent_key = key
            entries[self._metadata.name] = self._metadata
        return entries

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    def _attrs(self, inode: int, entry: Node | Scratch) -> pyfuse3.EntryAttributes:
        attrs = pyfuse3.EntryAttributes()
        attrs.st_ino = inode
        attrs.st_uid = self._uid
        attrs.st_gid = self._gid
        attrs.st_rdev = 0
        attrs.st_blksize = 4096

        if isinstance(entry, Scratch):
            if entry.is_dir:
                attrs.st_mode = stat.S_IFDIR | 0o755
                attrs.st_nlink = 2
                attrs.st_size = 0
            else:
                attrs.st_mode = stat.S_IFREG | (0o444 if entry.read_only else 0o644)
                attrs.st_nlink = 1
                attrs.st_size = entry.size
            mtime_ns = int(entry.mtime * 1e9)
        else:
            if entry.is_folder:
                attrs.st_mode = stat.S_IFDIR | 0o755
                attrs.st_nlink = 2
                attrs.st_size = 0
            else:
                writable = entry.writable and not self._read_only
                attrs.st_mode = stat.S_IFREG | (0o644 if writable else 0o444)
                attrs.st_nlink = 1
                attrs.st_size = entry.size
            mtime_ns = int(entry.mtime.timestamp() * 1e9)

        attrs.st_atime_ns = mtime_ns
        attrs.st_ctime_ns = mtime_ns
        attrs.st_mtime_ns = mtime_ns
        attrs.st_blocks = (attrs.st_size + 511) // 512

        # Matching the kernel's cache lifetime to the manifest's means a warm
        # mount does not ask us anything at all between refreshes.
        attrs.attr_timeout = self._ttl
        attrs.entry_timeout = self._ttl
        return attrs

    # ------------------------------------------------------------------
    # Lookup and directory reads
    # ------------------------------------------------------------------

    async def getattr(self, inode, ctx=None):
        await self._current()
        entry = self._resolve(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attrs(inode, entry)

    async def lookup(self, parent_inode, name, ctx=None):
        tree = await self._current()
        name = os.fsdecode(name)

        parent = self._resolve(parent_inode)
        if parent is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        if name == ".":
            return self._attrs(parent_inode, parent)
        if name == "..":
            if parent_inode == ROOT_INODE:
                return self._attrs(ROOT_INODE, parent)
            parent_key = self._parent_key(parent)
            if parent_key is None:
                raise pyfuse3.FUSEError(errno.ENOENT)
            grandparent = tree.get(parent_key) or self._scratch.get(parent_key)
            if grandparent is None:
                raise pyfuse3.FUSEError(errno.ENOENT)
            return self._attrs(self._inodes.inode_for(parent_key), grandparent)

        parent_key = self._inodes.key_for(parent_inode)
        entry = self._dir_entries(parent_key).get(name)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        inode = self._inodes.inode_for(entry.key)
        self._inodes.remember(inode)
        return self._attrs(inode, entry)

    async def forget(self, inode_list):
        for inode, count in inode_list:
            self._inodes.forget(inode, count)

    async def opendir(self, inode, ctx):
        await self._current()
        entry = self._resolve(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        is_dir = entry.is_dir if isinstance(entry, Scratch) else entry.is_folder
        if not is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        key = self._inodes.key_for(inode)
        # Snapshot now: readdir may be called several times for one handle and
        # the listing has to stay stable across them, whatever the TTL does.
        listing = sorted(self._dir_entries(key).items())
        fh = self._next_handle()
        self._handles[fh] = listing  # type: ignore[assignment]
        return fh

    async def readdir(self, fh, start_id, token):
        listing = self._handles.get(fh) or []
        for index in range(start_id, len(listing)):
            name, entry = listing[index]
            inode = self._inodes.inode_for(entry.key)
            if not pyfuse3.readdir_reply(
                token, os.fsencode(name), self._attrs(inode, entry), index + 1
            ):
                return
            self._inodes.remember(inode)

    async def releasedir(self, fh):
        self._handles.pop(fh, None)

    # ------------------------------------------------------------------
    # File reads
    # ------------------------------------------------------------------

    async def open(self, inode, flags, ctx):
        # ctx.pid is the only place a caller's identity is ever visible: read()
        # and write() carry a file handle and nothing else, so if we want to
        # know who is holding a file it has to be captured here, now, while the
        # caller is still blocked in the syscall and its pid cannot be reused.
        who = self._who(ctx)
        log.debug("open inode=%d flags=%#x uid=%d %s",
                  inode, flags, ctx.uid, procinfo.summarise(who))
        await self._current()
        entry = self._resolve(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        # Minted after the entry resolves, so the record names a document
        # rather than an inode, and before any of the checks below, so a
        # refused open is recorded too — an attempt on something you may not
        # have is the more interesting half of an access log. Scratch files are
        # local-only and never leave this process, so they are nobody's
        # business.
        #
        # Not queued yet: if this open goes on to fetch a body, the fetch
        # carries the event and the server records it itself. The `finally`
        # below spools it only if that did not happen, which covers every path
        # out of here including the refusals.
        event = None
        if self._audit is not None and isinstance(entry, Node):
            event = self._audit.event(document_id=entry.document_id,
                                      path=entry.path, open_flags=flags,
                                      process=who)
        carried = _Carrier(event)
        try:
            return await self._open(entry, flags, carried)
        finally:
            if event is not None and not carried.taken:
                self._audit.queue(event)

    async def _open(self, entry, flags, carried: "_Carrier"):
        if isinstance(entry, Node) and entry.is_folder:
            raise pyfuse3.FUSEError(errno.EISDIR)
        if isinstance(entry, Scratch) and entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)

        writing = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if writing and isinstance(entry, Scratch) and entry.read_only:
            raise pyfuse3.FUSEError(errno.EROFS)
        if writing and self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)
        if writing and isinstance(entry, Node) and not entry.writable:
            raise pyfuse3.FUSEError(errno.EACCES)

        # Before the content leaves us, not after: this is the revision the user
        # is about to base their edit on, whatever the tree says by the time they
        # save. It survives close/reopen deliberately, because an atomic save
        # writes a scratch file and renames it over the target — by then the
        # handle that read the original is long gone.
        #
        # O_TRUNC is the exception, and it is the whole point. A truncating open
        # hands the caller nothing, so it cannot have shown them a newer
        # revision — and it is exactly what an in-place editor save looks like.
        # Recording here would quietly re-base the edit onto whatever the poller
        # last pulled in, which is the lost update we are trying to prevent.
        if isinstance(entry, Node) and not flags & os.O_TRUNC:
            self._record_checkout(entry)

        # A truncating open is handed nothing, so there is nothing to fetch.
        # Loading and then discarding cost a round trip on every editor save,
        # and would have logged a read of bytes the caller never saw.
        if flags & os.O_TRUNC:
            data = bytearray()
        else:
            data = bytearray(await self._load(entry, carried))

        fh = self._next_handle()
        self._handles[fh] = Handle(
            key=entry.key,
            data=data,
            writable=writing,
            dirty=bool(flags & os.O_TRUNC) and writing,
        )
        return pyfuse3.FileInfo(fh=fh, keep_cache=False)

    async def read(self, fh, off, size):
        handle = self._handles.get(fh)
        if not isinstance(handle, Handle):
            raise pyfuse3.FUSEError(errno.EBADF)
        return bytes(handle.data[off:off + size])

    def _who(self, ctx) -> dict | None:
        """Identify the caller, if anyone is auditing."""
        if self._audit is None:
            return None
        return procinfo.describe(ctx.pid,
                                 full_cmdline=self._audit.full_cmdline)

    def _note(self, ctx, action: str, entry_or_path, document_id=None) -> None:
        """Record a mutation. Nothing carries these, so they go to the queue.

        `open` gets to ride along on the fetch that serves it; a create or a
        delete has no such request to attach to — the draft write it does make
        is a different transaction on a different table, and threading an audit
        event through `put_draft` would put the trail's shape into the
        publishing path for no gain.
        """
        if self._audit is None:
            return
        path = entry_or_path if isinstance(entry_or_path, str) else entry_or_path.path
        self._audit.record(document_id=document_id, path=path, action=action,
                           process=self._who(ctx))

    async def _load(self, entry: Node | Scratch,
                    carried: "_Carrier | None" = None) -> bytes:
        if isinstance(entry, Scratch):
            return bytes(entry.data)

        # A draft is the author's own work and always wins over the published
        # tip: that is what makes the working copy a working copy.
        #
        # Two columns, because a revision has two kinds of body and a draft of
        # one has to be able to hold either. Exactly one is ever set.
        if entry.draft is not None:
            if entry.draft.get("content_bytes") is not None:
                return entry.draft["content_bytes"]
            if entry.draft.get("content") is not None:
                return entry.draft["content"].encode("utf-8")
        if entry.document_id is None or not entry.published:
            return b""

        etag = f"v{entry.version}"
        cached = self._content.get(entry.key)
        if cached is not None and cached[0] == etag:
            # Served from our own cache, so the server never hears about this
            # read and cannot record it. The queue is the only route left, and
            # this is the main reason it still exists once the fetch can carry
            # an event by itself.
            return cached[1]

        # The audit event, if there is one, travels on this request: the read
        # and the record of it commit together, or neither does. `carried` is
        # marked taken so the caller does not also spool it.
        event = carried.event if carried is not None else None
        try:
            data = await self._client.content(entry.document_id, event=event)
        except LookupError:
            # The call itself succeeded and returned no rows, which means the
            # server ran the function and recorded the attempt. A read of
            # something you may not have is exactly the event worth keeping.
            if carried is not None:
                carried.taken = True
            raise pyfuse3.FUSEError(errno.ENOENT) from None
        except (PostgrestError, Unreachable) as exc:
            # Nothing committed, so the event did not land either and must go
            # to the queue. Leaving `taken` false is what arranges that.
            log.error("fetching %s: %s", entry.path, exc)
            raise pyfuse3.FUSEError(errno.EIO) from exc
        if carried is not None:
            carried.taken = True

        self._content[entry.key] = (etag, data)
        return data

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def write(self, fh, off, buf):
        handle = self._handles.get(fh)
        if not isinstance(handle, Handle):
            raise pyfuse3.FUSEError(errno.EBADF)
        if not handle.writable:
            raise pyfuse3.FUSEError(errno.EBADF)

        if off > len(handle.data):
            handle.data.extend(b"\0" * (off - len(handle.data)))
        handle.data[off:off + len(buf)] = buf
        handle.dirty = True
        return len(buf)

    async def setattr(self, inode, attr, fields, fh, ctx):
        entry = self._resolve(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        if fields.update_size:
            if isinstance(entry, Scratch) and entry.read_only:
                raise pyfuse3.FUSEError(errno.EROFS)
            size = attr.st_size
            if fh is not None and isinstance(self._handles.get(fh), Handle):
                handle = self._handles[fh]
                del handle.data[size:]
                handle.data.extend(b"\0" * (size - len(handle.data)))
                handle.dirty = True
            elif isinstance(entry, Scratch):
                del entry.data[size:]
                entry.data.extend(b"\0" * (size - len(entry.data)))

        # Mode, ownership and timestamps have nowhere to go: the wiki's
        # permissions are the ACL, not a mode word. Accepting them silently is
        # deliberate — editors chmod and utime as a matter of course, and
        # failing here makes files unsaveable for no benefit.
        return self._attrs(inode, entry)

    async def flush(self, fh):
        """Persist on close(), so the caller sees any error.

        release() would be too late: the kernel does not report its result to
        userspace, and a failed save that looks like a successful one is the
        worst outcome available.
        """
        handle = self._handles.get(fh)
        if not isinstance(handle, Handle) or not handle.dirty:
            return
        await self._persist(handle)
        handle.dirty = False

    async def fsync(self, fh, datasync):
        await self.flush(fh)

    async def release(self, fh):
        self._handles.pop(fh, None)

    async def _persist(self, handle: Handle) -> None:
        entry = self._scratch.get(handle.key)
        if entry is not None:
            entry.data = bytearray(handle.data)
            entry.mtime = anyio.current_time()
            return

        tree = self._tree
        node = tree.get(handle.key) if tree else None
        if node is None:
            raise pyfuse3.FUSEError(errno.ESTALE)

        await self._write_draft(node, bytes(handle.data))

    def _record_checkout(self, node: Node) -> None:
        """Remember which revision the user was shown for this document.

        Called wherever content is handed out. This is the value a later edit is
        based on, and it is emphatically *not* whatever the tree happens to say
        at save time — see _base_version_for().
        """
        draft_base = (node.draft or {}).get("base_version")
        if draft_base is not None:
            # Their draft descends from the revision it was first based on, not
            # from anything published since.
            self._checked_out[node.key] = draft_base
        elif node.version is not None:
            self._checked_out[node.key] = node.version

    def _base_version_for(self, node: Node) -> int | None:
        """The revision an edit to this node is actually based on.

        Reading it from the *current* tree is a silent lost update, and it is
        worth spelling out why. Open a file at revision 3; someone else
        publishes revision 4; the mount polls and refreshes; you save. The tree
        now says 4, so recording node.version would claim the edit was based on
        4 — push would accept it and revision 4 would be destroyed without
        anyone being told. The conflict machinery never runs, because we lied to
        it about what was edited.

        Precedence, strongest first:

        1. an existing draft's base_version — the content descends from it, and
           it must not drift as other people publish;
        2. the revision we last handed to this user, unless we have since
           published past it ourselves (see below);
        3. the tree's current revision, for a blind overwrite of a document the
           user never read. Fail-safe: at worst this is stale and produces a
           conflict, which is the direction to be wrong in.

        The exception in (2) is what keeps edit-push-edit from conflicting with
        itself. Push runs in the CLI, so the mount only learns about it from the
        manifest, and an in-place editor save truncates rather than re-reads —
        nothing would ever correct the remembered number. The tip's author
        separates the two cases exactly: if we published it, our copy of the
        file *is* that revision and the record can move on; if anyone else did,
        their revision is still something we have to be told about.
        """
        draft_base = (node.draft or {}).get("base_version")
        if draft_base is not None:
            return draft_base
        recorded = self._checked_out.get(node.key)
        if recorded is None:
            return node.version
        if (
            node.version is not None
            and node.version > recorded
            and node.version_author_id is not None
            and node.version_author_id == self._principal_id
        ):
            return node.version
        return recorded

    async def _write_draft(self, node: Node, data: bytes) -> None:
        """Record the buffer as this author's draft for the node's path."""
        if self._read_only or self._principal_id is None:
            raise pyfuse3.FUSEError(errno.EROFS)

        # Which column the body goes in follows from the content type, and it
        # is the only place in this file that has to know. `surrogateescape`
        # was never enough for a binary: it produces lone surrogates, which
        # neither UTF-8 nor JSON can carry back out, so a picture written
        # through the text column arrived corrupted or not at all.
        binary = naming.is_binary_type(node.content_type)
        text = None if binary else data.decode("utf-8", errors="surrogateescape")

        if node.document_id is None:
            operation, base_version = "create", None
        elif node.published:
            # Bypass the poll window before deciding the base. Everything below
            # reads the tip out of the tree, and a tree up to `poll` seconds old
            # gets that wrong in both directions: it can miss our own push and
            # invent a conflict, and it is the stale number an unread blind
            # overwrite would otherwise claim to descend from. Saves are rare
            # next to reads, and the check is a few bytes unless the wiki has
            # actually moved.
            self._checked_at = float("-inf")
            fresh = (await self._current()).get(node.key)
            if fresh is not None:
                node = fresh
            operation, base_version = "update", self._base_version_for(node)
        else:
            # A document row with no revision at all. wiki.draft's shape check
            # demands a base_version for 'update', and publish_revision demands
            # it match the live revision, which is none. There is no draft that
            # satisfies both, so this path cannot be edited through push.
            log.error("%s has no published revision; drafts cannot express an edit to it",
                      node.path)
            raise pyfuse3.FUSEError(errno.EPERM)

        try:
            await self._client.put_draft(
                author_id=self._principal_id,
                operation=operation,
                path=node.path,
                document_id=node.document_id,
                content=text,
                content_bytes=data if binary else None,
                content_type=node.content_type,
                base_version=base_version,
            )
        except (PostgrestError, Unreachable) as exc:
            log.error("saving draft for %s: %s", node.path, exc)
            raise pyfuse3.FUSEError(_errno_for(exc)) from exc

        await self.refresh(force=True)

    # ------------------------------------------------------------------
    # Creation, removal, renaming
    # ------------------------------------------------------------------

    def _next_handle(self) -> int:
        self._handle_seq += 1
        return self._handle_seq

    def _new_scratch(self, parent_key: str, name: str, *, is_dir: bool = False) -> Scratch:
        self._scratch_seq += 1
        scratch = Scratch(
            key=f"scratch:{self._scratch_seq}",
            parent_key=parent_key,
            name=name,
            is_dir=is_dir,
            mtime=anyio.current_time(),
        )
        self._scratch[scratch.key] = scratch
        return scratch

    async def create(self, parent_inode, name, mode, flags, ctx):
        tree = await self._current()
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

        name = os.fsdecode(name)
        parent_key = self._inodes.key_for(parent_inode)
        parent = self._resolve(parent_inode)
        if parent is None or parent_key is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if name in self._dir_entries(parent_key):
            raise pyfuse3.FUSEError(errno.EEXIST)

        parsed = naming.parse_filename(name)
        parent_path = parent.path if isinstance(parent, Node) else None

        if parsed is None or parent_path is None:
            # Either an unrepresentable name, or a directory that has no
            # server-side path yet. Both are local until a rename says otherwise.
            scratch = self._new_scratch(parent_key, name)
            inode = self._inodes.inode_for(scratch.key)
            self._inodes.remember(inode)
            fh = self._next_handle()
            self._handles[fh] = Handle(key=scratch.key, data=bytearray(), writable=True)
            return pyfuse3.FileInfo(fh=fh, keep_cache=False), self._attrs(inode, scratch)

        slug, content_type = parsed
        path = f"{parent_path}.{slug}"

        # Before the draft is written, on the same principle as open(): a
        # refused create is worth more than a successful one. There is no
        # document_id yet — that is what a create means — so the path carries
        # the identity.
        self._note(ctx, "create", path)

        try:
            # An empty body, in whichever column this type belongs to. The
            # draft shape check wants exactly one of them set, so a new
            # picture starts as zero bytes rather than as an empty string
            # that would later have to be told apart from one.
            empty = naming.is_binary_type(content_type)
            await self._client.put_draft(
                author_id=self._principal_id,
                operation="create",
                path=path,
                content=None if empty else "",
                content_bytes=b"" if empty else None,
                content_type=content_type,
            )
        except (PostgrestError, Unreachable) as exc:
            log.error("creating %s: %s", path, exc)
            raise pyfuse3.FUSEError(_errno_for(exc)) from exc

        tree = await self.refresh(force=True)
        node = tree.get(f"draft:{path}")
        if node is None:
            raise pyfuse3.FUSEError(errno.EIO)

        inode = self._inodes.inode_for(node.key)
        self._inodes.remember(inode)
        fh = self._next_handle()
        self._handles[fh] = Handle(key=node.key, data=bytearray(), writable=True)
        return pyfuse3.FileInfo(fh=fh, keep_cache=False), self._attrs(inode, node)

    async def mkdir(self, parent_inode, name, mode, ctx):
        """Local only.

        A folder has no independent existence to create: `wiki.push()`
        materialises every folder on the path of a document it publishes. So a
        new directory lives here until something inside it is pushed, at which
        point the server's own folder takes over.
        """
        await self._current()
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

        name = os.fsdecode(name)
        parent_key = self._inodes.key_for(parent_inode)
        if parent_key is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if name in self._dir_entries(parent_key):
            raise pyfuse3.FUSEError(errno.EEXIST)

        scratch = self._new_scratch(parent_key, name, is_dir=True)
        inode = self._inodes.inode_for(scratch.key)
        self._inodes.remember(inode)
        return self._attrs(inode, scratch)

    async def unlink(self, parent_inode, name, ctx):
        tree = await self._current()
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

        name = os.fsdecode(name)
        parent_key = self._inodes.key_for(parent_inode)
        entry = self._dir_entries(parent_key or "").get(name)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        if isinstance(entry, Scratch):
            if entry.read_only:
                raise pyfuse3.FUSEError(errno.EROFS)
            if entry.is_dir:
                raise pyfuse3.FUSEError(errno.EISDIR)
            del self._scratch[entry.key]
            return

        if entry.is_folder:
            raise pyfuse3.FUSEError(errno.EISDIR)

        # Scratch files are gone by here, so this is a real document: either a
        # draft being withdrawn or a published page being retired. Recorded
        # ahead of the capability check, so a refused delete leaves a mark too.
        self._note(ctx, "delete", entry, entry.document_id)

        # An unpublished draft is withdrawn outright; a published document gets
        # a delete draft, which push turns into a tombstone.
        if entry.document_id is None:
            await self._drop_draft(entry.path)
            return
        if not entry.published:
            raise pyfuse3.FUSEError(errno.EPERM)
        if "delete" not in entry.capabilities:
            raise pyfuse3.FUSEError(errno.EACCES)

        try:
            await self._client.put_draft(
                author_id=self._principal_id,
                operation="delete",
                path=entry.path,
                document_id=entry.document_id,
                base_version=entry.version,
            )
        except (PostgrestError, Unreachable) as exc:
            log.error("retiring %s: %s", entry.path, exc)
            raise pyfuse3.FUSEError(errno.EIO) from exc
        await self.refresh(force=True)

    async def _drop_draft(self, path: str) -> None:
        try:
            await self._client.delete_draft(path)
        except (PostgrestError, Unreachable) as exc:
            log.error("dropping draft %s: %s", path, exc)
            raise pyfuse3.FUSEError(errno.EIO) from exc
        await self.refresh(force=True)

    async def rmdir(self, parent_inode, name, ctx):
        await self._current()
        name = os.fsdecode(name)
        parent_key = self._inodes.key_for(parent_inode)
        entry = self._dir_entries(parent_key or "").get(name)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if isinstance(entry, Node):
            # Folder restructuring is an 'administer' operation and push has no
            # implementation for it yet.
            raise pyfuse3.FUSEError(errno.EPERM)
        if not entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if self._dir_entries(entry.key):
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        del self._scratch[entry.key]

    async def rename(self, parent_old, name_old, parent_new, name_new, flags, ctx):
        """The operation that makes editors work.

        An atomic save is `write(tmp); rename(tmp, target)`, and `tmp` is a name
        the server cannot hold. Handling it here is what turns a temp file's
        bytes into a draft on the real path.
        """
        if flags != 0:
            raise pyfuse3.FUSEError(errno.EINVAL)

        tree = await self._current()
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

        name_old = os.fsdecode(name_old)
        name_new = os.fsdecode(name_new)
        old_parent_key = self._inodes.key_for(parent_old)
        new_parent_key = self._inodes.key_for(parent_new)
        if old_parent_key is None or new_parent_key is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        source = self._dir_entries(old_parent_key).get(name_old)
        if source is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if isinstance(source, Scratch) and source.read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

        destination = self._dir_entries(new_parent_key).get(name_new)
        if isinstance(destination, Scratch) and destination.read_only:
            raise pyfuse3.FUSEError(errno.EROFS)
        new_parent = self._resolve(parent_new)
        parsed = naming.parse_filename(name_new)
        parent_path = new_parent.path if isinstance(new_parent, Node) else None

        # Staying local: an unrepresentable target name, or a directory with no
        # server path. Just move the scratch entry.
        if parsed is None or parent_path is None:
            if not isinstance(source, Scratch):
                raise pyfuse3.FUSEError(errno.EPERM)
            if isinstance(destination, Scratch):
                del self._scratch[destination.key]
            source.parent_key = new_parent_key
            source.name = name_new
            return

        slug, content_type = parsed
        target_path = f"{parent_path}.{slug}"

        if isinstance(source, Scratch):
            if source.is_dir:
                raise pyfuse3.FUSEError(errno.EPERM)
            # The interesting case: buffered bytes become a draft. If something
            # is already published there this is an edit, otherwise a create.
            target_node = tree.get(tree.by_path.get(target_path, ""))
            data = bytes(source.data)
            if target_node is not None and target_node.document_id and target_node.published:
                # The most consequential event the mount can record. write()
                # carries no caller, so this rename is the last point at which
                # "who is editing this" is still knowable.
                self._note(ctx, "write", target_path, target_node.document_id)
                await self._write_draft(target_node, data)
                new_key = target_node.key
            else:
                # Same rename, but nothing is published there: the save is
                # bringing a document into existence rather than editing one.
                self._note(ctx, "create", target_path)
                await self._create_draft(target_path, content_type, data)
                new_key = f"draft:{target_path}"

            # After a rename the kernel keeps the *source* inode and files it
            # under the new name — it does not re-look-up. So the scratch file's
            # inode has to start resolving to whatever now lives at the target
            # path, or the file the caller just saved reads back as ENOENT.
            del self._scratch[source.key]
            self._inodes.rekey(source.key, new_key)
            return

        if source.is_folder:
            raise pyfuse3.FUSEError(errno.EPERM)
        if destination is not None:
            raise pyfuse3.FUSEError(errno.EEXIST)

        if source.document_id is None:
            # Renaming a draft that was never published: rewrite it in place.
            await self._create_draft(target_path, content_type,
                                     draft_body(source.draft or {}))
            await self._drop_draft(source.path)
            # And the same rekey the scratch branch does above, for the same
            # reason: the kernel keeps the *source* inode and files it under the
            # new name rather than looking the new name up. Without this the
            # inode still points at `draft:<old path>`, which no longer
            # resolves, and the file the caller just renamed reads back ENOENT.
            #
            # It hid behind the clock. Every read after a rename went through a
            # fresh lookup because the kernel's attribute TTL had expired while
            # the manifest was being fetched; once that fetch stopped costing
            # ~90 ms, the read landed inside the TTL and the stale inode was
            # used. So this was a latent bug that a faster ACL turned into a
            # deterministic one.
            self._inodes.rekey(source.key, f"draft:{target_path}")
            return

        if not source.published:
            raise pyfuse3.FUSEError(errno.EPERM)

        # A real document changing path. Recorded against the destination,
        # since that is what the ACL will be asked about.
        self._note(ctx, "move", target_path, source.document_id)

        try:
            await self._client.put_draft(
                author_id=self._principal_id,
                operation="move",
                path=target_path,
                document_id=source.document_id,
                base_version=source.version,
            )
        except (PostgrestError, Unreachable) as exc:
            log.error("moving %s -> %s: %s", source.path, target_path, exc)
            raise pyfuse3.FUSEError(errno.EIO) from exc
        await self.refresh(force=True)

    async def _create_draft(self, path: str, content_type: str, data: bytes) -> None:
        try:
            await self._client.put_draft(
                author_id=self._principal_id,
                operation="create",
                path=path,
                content=data.decode("utf-8", errors="surrogateescape"),
                content_type=content_type,
            )
        except (PostgrestError, Unreachable) as exc:
            log.error("creating draft %s: %s", path, exc)
            raise pyfuse3.FUSEError(_errno_for(exc)) from exc
        await self.refresh(force=True)

    # ------------------------------------------------------------------
    # Extended attributes: the ACL, readable from the shell
    # ------------------------------------------------------------------

    def _xattrs(self, entry: Node | Scratch) -> dict[str, str]:
        if isinstance(entry, Scratch):
            return {"state": "scratch (local only, never pushed)"}
        values = {
            "path": entry.path,
            "capabilities": ",".join(sorted(entry.capabilities)),
            "state": "draft" if entry.has_draft else "published",
        }
        if entry.document_id:
            values["document_id"] = entry.document_id
        if entry.version is not None:
            values["version"] = str(entry.version)
        if entry.owner_id:
            values["owner_id"] = entry.owner_id
        if entry.title:
            values["title"] = entry.title
        if entry.synthetic:
            values["state"] = "synthetic (implied by a draft below it)"
        if entry.draft:
            values["draft_operation"] = entry.draft["operation"]
            # A conflicted draft looks like any other file — the markers are in
            # the text and nothing else says so. Saying it here is the only way
            # the mount can tell you, short of inventing a filename convention.
            if entry.draft.get("state") == "conflicted":
                values["state"] = (
                    f"conflicted (merged with revision "
                    f"{entry.draft.get('merged_from')}; resolve the markers, "
                    f"or run: fswiki merge --abort)"
                )
        return values

    async def listxattr(self, inode, ctx):
        entry = self._resolve(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return [os.fsencode(XATTR_PREFIX + k) for k in self._xattrs(entry)]

    async def getxattr(self, inode, name, ctx):
        entry = self._resolve(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        key = os.fsdecode(name)
        if not key.startswith(XATTR_PREFIX):
            raise pyfuse3.FUSEError(pyfuse3.ENOATTR)
        value = self._xattrs(entry).get(key[len(XATTR_PREFIX):])
        if value is None:
            raise pyfuse3.FUSEError(pyfuse3.ENOATTR)
        return os.fsencode(value)

    async def setxattr(self, inode, name, value, ctx):
        # Administering the ACL through xattrs is the CLI's job and needs a
        # grammar; refusing plainly beats accepting and discarding.
        raise pyfuse3.FUSEError(errno.ENOTSUP)

    # ------------------------------------------------------------------

    async def statfs(self, ctx):
        info = pyfuse3.StatvfsData()
        info.f_bsize = 4096
        info.f_frsize = 4096
        info.f_blocks = 0
        info.f_bfree = 0
        info.f_bavail = 0
        info.f_files = len(self._tree.nodes) if self._tree else 0
        info.f_ffree = 0
        info.f_favail = 0
        info.f_namemax = 255
        return info
