"""Filenames on one side, ltree slugs on the other.

The server constrains a slug to `^[^./\\[:space:]]+$`, at most 255 characters:
no dot, no slash, no backslash, no whitespace. That a slug can never contain a
dot is what makes the mapping reversible — `guide.md` is unambiguously the slug
`guide` carrying the extension `.md`, and there is no filename that could be
read two ways.

Anything that is not a valid slug is *scratch*: a local-only file the client
keeps in memory and never sends anywhere. That is not a workaround, it is what
makes ordinary editors work. vim, emacs and VS Code all save by writing
`.file.swp` / `file~` / `file.tmp` alongside the target and renaming over it,
and every one of those names is unrepresentable server-side.
"""

from __future__ import annotations

import re

# Mirrors wiki.document_slug_shape. Python's \s is close enough to POSIX
# [:space:] for the characters a filesystem can actually deliver.
_SLUG_RE = re.compile(r"^[^./\\\s]+$")

# Extension by content type, and the reverse. The first entry for a type wins
# when going from type to extension.
#
# One map, holding both kinds of body. It was two for as long as an attachment
# was a row in a table of its own: `parse_filename` decides what a file written
# into the mount *means*, and while the mount could not carry bytes, a
# `logo.png` saved into a directory had to stay scratch rather than become a
# document claiming to be an image with text in it.
#
# A file is a revision now, so writing one into the mount is exactly as
# meaningful as writing a page, and the split had nothing left to protect.
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("text/markdown", ".md"),
    ("text/plain", ".txt"),
    ("text/html", ".html"),
    ("text/x-rst", ".rst"),
    ("application/json", ".json"),
    ("text/csv", ".csv"),
    ("text/yaml", ".yaml"),
    # Binary from here down. The list is short on purpose: every entry is a
    # type a browser will be asked to display or download, so each one is a
    # decision. `fswiki_core.pages.INLINE` narrows it again to what may render
    # inside a page rather than download.
    ("image/png", ".png"),
    # One extension per type, and no aliases. `.jpeg` is missing on purpose:
    # with both, `photo.jpeg` would parse to image/jpeg and print back as
    # `photo.jpg`, and the mount would appear to rename somebody's file after
    # a refresh. test_naming.py asserts the round trip for every entry here,
    # which is what caught it. A name we do not recognise stays scratch, which
    # is the documented and survivable answer.
    ("image/jpeg", ".jpg"),
    ("image/gif", ".gif"),
    ("image/webp", ".webp"),
    ("image/avif", ".avif"),
    ("image/svg+xml", ".svg"),
    ("application/pdf", ".pdf"),
    ("application/zip", ".zip"),
    ("audio/mpeg", ".mp3"),
    ("video/mp4", ".mp4"),
)

#: The types whose body is text. Everything else this wiki knows about is
#: bytes, and the difference decides which column a revision fills, whether a
#: three-way merge is possible at all, and what the mount hands the kernel.
#:
#: An allowlist rather than `startswith("text/")`, because `application/json`
#: is text and `image/svg+xml` is not, and neither is guessable from the
#: prefix.
TEXTUAL: frozenset[str] = frozenset({
    "text/markdown", "text/plain", "text/html", "text/x-rst",
    "application/json", "text/csv", "text/yaml",
})

EXT_BY_TYPE: dict[str, str] = {}
for _type, _ext in _EXTENSIONS:
    EXT_BY_TYPE.setdefault(_type, _ext)

TYPE_BY_EXT: dict[str, str] = {ext: typ for typ, ext in _EXTENSIONS}

DEFAULT_CONTENT_TYPE = "text/markdown"


def is_binary_type(content_type: str | None) -> bool:
    """Whether a body of this type is bytes rather than text.

    A type nobody listed is binary, which is the safe direction: text treated
    as bytes round-trips exactly, and bytes treated as text do not survive the
    trip at all.
    """
    return (content_type or DEFAULT_CONTENT_TYPE) not in TEXTUAL


def is_slug(value: str) -> bool:
    """Would the server accept this as a slug?

    Length is counted in bytes to match both `document_slug_shape` and the
    kernel: NAME_MAX is 255 *bytes*, so a 255-character CJK name is three times
    too long for a filename however short it looks.
    """
    return (
        bool(value)
        and len(value.encode("utf-8")) <= 255
        and _SLUG_RE.match(value) is not None
    )


def filename(slug: str, content_type: str | None, is_folder: bool) -> str:
    """The name this document appears under in a directory listing."""
    if is_folder:
        return slug
    return slug + EXT_BY_TYPE.get(content_type or DEFAULT_CONTENT_TYPE, "")


def parse_filename(name: str) -> tuple[str, str] | None:
    """Split a filename into (slug, content_type), or None if it is not one.

    A name with no extension is taken as markdown rather than rejected, so
    `touch notes` does the obvious thing. A name whose extension we do not
    recognise is scratch — guessing a content type from an arbitrary suffix
    would let `report.tar.gz` become the slug `report`.
    """
    if "." not in name:
        return (name, DEFAULT_CONTENT_TYPE) if is_slug(name) else None

    slug, _, ext = name.partition(".")
    content_type = TYPE_BY_EXT.get("." + ext)
    if content_type is None or not is_slug(slug):
        return None
    return slug, content_type


def ltree_labels(path: str) -> list[str]:
    """`root.a.b` -> ['root', 'a', 'b']. Exact, because slugs hold no dots."""
    return path.split(".")


def ltree_parent(path: str) -> str | None:
    head, sep, _ = path.rpartition(".")
    return head if sep else None


def to_display(path: str) -> str:
    """`root.public.welcome` -> `public/welcome`. The inverse of from_display().

    Without the extension: the content type is not in the path, and guessing
    one for a title or a URL would be worse than leaving it off.
    """
    labels = ltree_labels(path)
    if labels and labels[0] == "root":
        labels = labels[1:]
    return "/".join(labels) or "/"


def looks_like_ltree(value: str) -> bool:
    """Whether something a person typed is already a wiki path."""
    return value.startswith("root.") or value == "root"


def from_display(path: str) -> str:
    """`public/welcome` -> `root.public.welcome`.

    The inverse of a display path: slash-separated, `root` optional, and no
    extension expected — a wikilink names a document, and the content type is
    not part of a document's identity.

    A trailing extension is accepted and dropped, so `[[public/welcome.md]]`
    and `[[public/welcome]]` mean the same thing. Anything else that is not a
    slug raises, rather than being coerced into one: a link to a name the wiki
    could never hold should stay literal text.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if parts and parts[0] == "root":
        parts = parts[1:]
    if not parts:
        return "root"

    labels = []
    for index, part in enumerate(parts):
        if index == len(parts) - 1 and "." in part:
            parsed = parse_filename(part)
            if parsed is None:
                raise ValueError(f"{part!r} is not a name the wiki can hold")
            labels.append(parsed[0])
            continue
        if not is_slug(part):
            raise ValueError(f"{part!r} is not a valid path element")
        labels.append(part)
    return ".".join(["root", *labels])
