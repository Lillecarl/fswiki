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

# Media types an attachment may be served under, and the extension each shows.
#
# A separate map from the one above, and that is not tidiness. `_EXTENSIONS`
# holds *document* content types, and `parse_filename` uses it to decide what a
# file written into the mount means. Put `.png` in there and a `logo.png`
# saved into a directory becomes a document claiming to be an image, with text
# in it. A binary file appearing in the mount must stay scratch until the FUSE
# driver can carry bytes.
#
# The list is short on purpose. It is what a browser is asked to display or
# download, so every entry is a decision; `fswiki_core.pages.INLINE` narrows it
# again to what may render in the page rather than download.
_ATTACHMENT_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("image/png", ".png"),
    ("image/jpeg", ".jpg"),
    ("image/jpeg", ".jpeg"),
    ("image/gif", ".gif"),
    ("image/webp", ".webp"),
    ("image/avif", ".avif"),
    ("image/svg+xml", ".svg"),
    ("application/pdf", ".pdf"),
    ("application/zip", ".zip"),
    ("audio/mpeg", ".mp3"),
    ("video/mp4", ".mp4"),
)

ATTACHMENT_EXT_BY_TYPE: dict[str, str] = {}
for _type, _ext in _ATTACHMENT_EXTENSIONS:
    ATTACHMENT_EXT_BY_TYPE.setdefault(_type, _ext)

ATTACHMENT_TYPE_BY_EXT: dict[str, str] = {
    ext: typ for typ, ext in _ATTACHMENT_EXTENSIONS}


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


def attachment_filename(slug: str, media_type: str | None) -> str:
    """What an attachment is called, for a URL and for a download.

    An unknown media type gets no extension rather than a guessed one. The
    browser is told the type in a header either way, and inventing `.bin` would
    put a lie in the filename a person saves.
    """
    return slug + ATTACHMENT_EXT_BY_TYPE.get(media_type or "", "")


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


def from_route(path: str) -> str:
    """A browser URL to a wiki path. `from_display`, plus attachment names.

    Separate because the two callers want different things from an unknown
    extension. A wikilink to `report.tar.gz` should stay literal text, so
    `from_display` raises. A *URL* ending in `.png` is somebody asking for a
    file, and refusing it would mean an attachment could only be linked
    without the extension a browser and a download both expect.

    Only the extensions this wiki actually serves are stripped, so the two
    functions still agree that `report.tar.gz` is not a name the wiki holds.
    """
    head, _, tail = path.replace("\\", "/").rpartition("/")
    stem, dot, ext = tail.rpartition(".")
    if dot and ("." + ext) in ATTACHMENT_TYPE_BY_EXT and is_slug(stem):
        tail = stem
    return from_display(f"{head}/{tail}" if head else tail)
