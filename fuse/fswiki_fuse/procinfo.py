"""Who asked. Reads /proc for the process behind a FUSE request.

Only `open`, `lookup`, `getattr`, `create`, `setattr`, `unlink` and `rename`
carry a RequestContext; `read` and `write` get a file handle and nothing else.
So the caller can only be identified when a file is *opened*, and the audit
granularity that follows is "this process opened this document", never "this
process read these bytes".

Reading it inline is safe, and for one specific reason: the caller is blocked in
the syscall waiting for our reply, so it cannot have exited and its pid cannot
have been reused underneath us. The same read done later, off a queue, is a
race against both. It is also cheap — the whole bundle is ~32us against the
~30ms HTTP fetch open() already blocks on, so there is nothing to defer.

**None of this is evidence.** The mount runs on the user's own laptop, so
everything here is a claim by software they control, and two of the fields are
forgeable without even trying:

* `cmdline` is argv, which a process may rewrite in place;
* `comm` is settable with prctl(PR_SET_NAME).

`exe` is a kernel-maintained symlink to the actual inode and cannot be pointed
elsewhere by the process itself, and `loginuid` is stamped by PAM at login and
needs CAP_AUDIT_CONTROL to change. Those two are worth something. The rest is a
hint that is useful on a managed fleet and worthless against an adversary who
owns the machine — the authoritative record of who read what is server-side,
against the token.
"""

from __future__ import annotations

import os

# argv can be megabytes. Nobody auditing a wiki needs that much of it.
CMDLINE_LIMIT = 4096


def _read(path: str, limit: int = 65536) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        # Process gone, or /proc not visible from this namespace. Both are
        # normal; neither is worth failing an open() over.
        return None


def _text(raw: bytes | None) -> str | None:
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace").strip() or None


def describe(pid: int, *, full_cmdline: bool = False) -> dict | None:
    """A best-effort identity for `pid`, or None if it cannot be read.

    Returns only the fields that were actually available, so a caller in a
    foreign pid namespace degrades to a bare pid rather than to a lie.

    **argv is truncated to argv[0] unless `full_cmdline`.** Command lines are
    where people put secrets — `mysql -pSECRET`, `curl -H "Authorization: ..."`,
    an API key passed to a one-off script — and none of it has anything to do
    with the wiki. Shipping it would take secrets off the user's machine and
    put them in someone else's database, and then in that database's backups,
    which is a worse leak than the one an audit trail is meant to catch. The
    program's identity is what the record is for, and argv[0] plus `exe` says
    that. What was dropped is counted rather than silently omitted, so a reader
    can tell a bare command from a truncated one.
    """
    if pid <= 0:
        # The kernel reports 0 for requests it raised itself — readahead,
        # writeback, and anything else with no process behind it.
        return None

    base = f"/proc/{pid}"
    info: dict = {"pid": pid}

    cmdline = _read(f"{base}/cmdline", CMDLINE_LIMIT)
    if cmdline:
        # NUL-separated, usually with a trailing NUL.
        argv = [
            a.decode("utf-8", errors="replace")
            for a in cmdline.split(b"\0")
            if a
        ]
        if argv:
            info["cmdline"] = argv if full_cmdline else argv[:1]
            if not full_cmdline and len(argv) > 1:
                info["argv_elided"] = len(argv) - 1

    comm = _text(_read(f"{base}/comm", 64))
    if comm:
        info["comm"] = comm

    try:
        info["exe"] = os.readlink(f"{base}/exe")
    except OSError:
        pass

    # Field 4 is ppid and field 22 is starttime, in clock ticks since boot.
    # starttime is what makes the record survive pid reuse: the pair
    # (pid, starttime) identifies one process for as long as the box is up.
    # comm sits in field 2 wrapped in parentheses and may itself contain
    # spaces and parentheses, so split from the last ')' rather than the first.
    stat = _read(f"{base}/stat", 4096)
    if stat:
        try:
            fields = stat[stat.rindex(b")") + 2:].split()
            info["ppid"] = int(fields[1])
            info["starttime"] = int(fields[19])
        except (ValueError, IndexError):
            pass

    # Stamped by PAM at login and immutable without CAP_AUDIT_CONTROL, so it
    # survives su/sudo and says which human's session this descends from.
    # 4294967295 is the unset sentinel.
    loginuid = _text(_read(f"{base}/loginuid", 32))
    if loginuid and loginuid.isdigit() and int(loginuid) != 0xFFFFFFFF:
        info["loginuid"] = int(loginuid)

    return info


def summarise(info: dict | None) -> str:
    """One line, for logs."""
    if not info:
        return "unknown"
    name = info.get("comm") or "?"
    argv = " ".join(info.get("cmdline") or ())
    elided = info.get("argv_elided")
    if elided:
        argv = f"{argv} (+{elided} args)"
    return f"{name}[{info['pid']}] {argv}".rstrip()
