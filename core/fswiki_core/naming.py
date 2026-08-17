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
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("text/markdown", ".md"),
    ("text/plain", ".txt"),
    ("text/html", ".html"),
    ("text/x-rst", ".rst"),
    ("application/json", ".json"),
    ("text/csv", ".csv"),
    ("text/yaml", ".yaml"),
)

EXT_BY_TYPE: dict[str, str] = {}
for _type, _ext in _EXTENSIONS:
    EXT_BY_TYPE.setdefault(_type, _ext)

TYPE_BY_EXT: dict[str, str] = {ext: typ for typ, ext in _EXTENSIONS}

DEFAULT_CONTENT_TYPE = "text/markdown"


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
