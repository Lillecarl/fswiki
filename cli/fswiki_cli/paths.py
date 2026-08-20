"""Turning what someone types into the ltree path the server wants.

Three forms are accepted, in this order of preference:

1. **A file inside a mount.** The root's `.fswiki` metadata identifies the mount
   and its server; the document path is relative to that root. Linux xattrs are
   retained as a compatibility shortcut.
2. **An ltree path**, `root.public.welcome`, passed straight through.
3. **A filesystem-looking path**, `public/welcome.md`, converted by stripping the
   extension and joining the parts with dots.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path

from fswiki_core import naming

XATTR_PATH = "user.fswiki.path"
MOUNT_FILE = ".fswiki"


@dataclass(frozen=True)
class MountPath:
    path: str
    url: str
    root: Path


class PathError(ValueError):
    pass


def from_xattr(value: str) -> str | None:
    """The document path recorded on a file by the FUSE client, if there is one."""
    try:
        raw = os.getxattr(value, XATTR_PATH)
    except (OSError, AttributeError):
        return None
    return raw.decode("utf-8", errors="replace") or None


def from_mount(value: str) -> MountPath | None:
    """Resolve an existing path through the nearest fswiki mount marker."""
    target = Path(value).expanduser()
    if not target.exists():
        return None
    target = target.resolve()
    start = target if target.is_dir() else target.parent
    for directory in (start, *start.parents):
        marker = directory / MOUNT_FILE
        try:
            metadata = json.loads(marker.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if (metadata.get("format") != "fswiki-mount"
                or metadata.get("version") != 1
                or not isinstance(metadata.get("url"), str)):
            continue
        relative = target.relative_to(directory)
        if not relative.parts:
            path = "root"
        else:
            path = from_filesystem(str(relative))
        return MountPath(path=path, url=metadata["url"], root=directory)
    return None


def from_filesystem(value: str) -> str:
    """`public/welcome.md` -> `root.public.welcome`.

    A leading `root/` or `/` is tolerated so that both what `ls` prints and what
    a shell completes are accepted.
    """
    parts = [p for p in Path(value).parts if p not in ("/", ".", "")]
    if parts and parts[0] == "root":
        parts = parts[1:]
    if not parts:
        return "root"

    labels = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if last:
            parsed = naming.parse_filename(part)
            if parsed is None:
                raise PathError(
                    f"{part!r} is not a name the wiki can hold — slugs may not "
                    f"contain dots, slashes or whitespace"
                )
            labels.append(parsed[0])
        else:
            if not naming.is_slug(part):
                raise PathError(f"{part!r} is not a valid folder name")
            labels.append(part)
    return ".".join(["root", *labels])


def resolve(value: str) -> str:
    """Best available interpretation of one user-supplied path."""
    mounted = from_mount(value)
    if mounted:
        return mounted.path
    recorded = from_xattr(value)
    if recorded:
        return recorded
    if looks_like_ltree(value):
        return value
    return from_filesystem(value)


# Both of these are core's now: a browser needs them as much as a terminal
# does, and the browser-facing server has no business importing the CLI. Kept
# as names here because this is where the rest of the module's callers look.
looks_like_ltree = naming.looks_like_ltree
to_display = naming.to_display
