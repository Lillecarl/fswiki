"""The little a document is allowed to say about its own shell.

A markdown document opens with a fenced block, and an author gets one key::

    ---
    layout: wide
    ---
    # Hi

This runs **before** the backend, not inside it. Two reasons, and the second
is the one that decided it:

* `to_html(text) -> str` returns HTML and nothing else. Widening it to return
  metadata would touch every backend and change what the cache key means.
* A `---` block that reaches a markdown engine renders as a horizontal rule
  followed by a paragraph of keys. Stripping it is not tidiness; it is the
  difference between frontmatter and visible noise.

So the pipeline gains a step at the front:

    split() -> [[wikilinks]] -> backend.to_html() -> sanitise

**This is a security surface, not a formatting convenience.** One person
writes the frontmatter and another reads the page it shapes, which is the
same sentence that governs `render.safety`. But safety sanitises the *body*,
and the shell is composed after it -- so nothing here may reach an attribute,
a class name or a stylesheet. The defence is the return type: `Options` has
one field, it holds one of two values fixed in this file, and a document
cannot add a second field to a frozen dataclass. A key nobody here knows is
not rejected with a message, it simply has nowhere to go.

The banner is the thing this must never touch. `pages.Pages.shell()` puts
"viewing as X" on every impersonated page because the whole failure mode of
impersonation is forgetting you are doing it. No value here suppresses it,
and `test_render_frontmatter.py` says so out loud.

**No YAML parser.** `layout: wide` is the entire grammar, and a real YAML
parser would additionally accept anchors, aliases, tags, nesting and
multi-document streams -- every one of which this file would then throw away.
A parser for the language you accept is smaller and more honest than a
parser for a language you refuse. It also keeps `fswiki-core` free of a
dependency it would use for one line.

Both formats fail the same way: anything unexpected means "there is no
frontmatter here", the text is returned untouched, and the page renders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["LAYOUTS", "Options", "split"]

#: Every layout there is. The value in the document selects one of these; it
#: never becomes one. `pages` owns what each means.
LAYOUTS = ("default", "wide")

# `key: value`, one line, nothing nested. The colon is required, so a stray
# `---` fence around prose fails to parse and the prose survives -- see
# `_yaml_block`.
_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*?)[ \t]*$")

# reStructuredText's own docinfo, which this reads rather than replaces.
_DOCINFO = re.compile(r"^:([A-Za-z_][A-Za-z0-9_ -]*):[ \t]*(.*?)[ \t]*$")

# The characters docutils accepts as a section underline. A title may sit
# above the field list, and usually does.
_RULE = re.compile(r"^([!-/:-@\[-`{-~])\1+[ \t]*$")

_QUOTES = ("''", '""')


@dataclass(frozen=True)
class Options:
    """What a document asked for, after everything unknown fell away.

    Frozen and fully defaulted, so "the document said nothing" and "the
    document said something we do not implement" are the same object rather
    than two code paths.
    """

    #: One of `LAYOUTS`. Never a class name, never a width.
    layout: str = "default"


def split(text: str, content_type: str = "text/markdown") -> tuple[Options, str]:
    """`(options, text)`, with the frontmatter removed from the text.

    Never raises and never loses content. If the head of the document is not
    frontmatter -- malformed, unterminated, or simply a horizontal rule --
    the answer is the default options and `text` exactly as it came in.
    """
    if content_type == "text/x-rst":
        fields, rest = _docinfo(text)
    else:
        fields, rest = _yaml_block(text)
    return _options(fields), rest


def _options(fields: dict[str, str]) -> Options:
    """The allowlist, applied. Every unknown key and value ends up here."""
    layout = fields.get("layout", "").lower()
    return Options(layout=layout if layout in LAYOUTS else "default")


def _value(raw: str) -> str:
    """One scalar. Quotes come off; nothing else happens to it."""
    for pair in _QUOTES:
        if len(raw) >= 2 and raw[0] == pair[0] and raw[-1] == pair[1]:
            return raw[1:-1]
    return raw


def _yaml_block(text: str) -> tuple[dict[str, str], str]:
    """A `---` block at the very top, for markdown.

    All or nothing on purpose. A document that opens with a horizontal rule
    and closes a thought with another one looks exactly like frontmatter from
    the first line, so a single line that is not a field disqualifies the
    whole block and the document is returned untouched. Swallowing a
    paragraph would be a far worse failure than ignoring a `layout:`.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if stripped in ("---", "..."):
            # The closing fence. Everything after it is the document, and the
            # newline that followed the fence belongs to the fence.
            return fields, "\n".join(lines[index + 1:])
        if not stripped or stripped.startswith("#"):
            continue
        match = _FIELD.match(line)
        if match is None:
            return {}, text
        fields[match.group(1).lower()] = _value(match.group(2))
    # Ran off the end without a closing fence, so it was never a block.
    return {}, text


def _docinfo(text: str) -> tuple[dict[str, str], str]:
    """reStructuredText's field list, which docutils already understands.

    rST has carried document metadata since long before this project, so it
    gets no second mechanism -- `:layout: wide` is the same thing an author
    writes for `:Author:`. Only the keys named here are taken out of the
    text; every other field stays where it is and renders as the docinfo
    table docutils would have made of it anyway.

    The field list may follow a title, and usually does. It may not follow a
    paragraph: a field list further down the document is body text, not
    metadata, and docutils treats it that way too.
    """
    lines = text.split("\n")
    cursor = _skip_title(lines)

    fields: dict[str, str] = {}
    consumed: list[int] = []
    while cursor < len(lines):
        match = _DOCINFO.match(lines[cursor])
        if match is None:
            break
        name = match.group(1).strip().lower()
        fields[name] = _value(match.group(2))
        if name in _RECOGNISED:
            consumed.append(cursor)
        cursor += 1

    if not consumed:
        return fields, text
    kept = [line for number, line in enumerate(lines) if number not in set(consumed)]
    return fields, "\n".join(kept)


def _skip_title(lines: list[str]) -> int:
    """The first line after any leading blank lines and an optional title.

    Both forms docutils accepts: an underlined title, and one with a matching
    overline above it.
    """
    cursor = 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    # Overlined: rule, text, rule.
    if (cursor + 2 < len(lines) and _RULE.match(lines[cursor])
            and lines[cursor + 1].strip() and _RULE.match(lines[cursor + 2])):
        cursor += 3
    # Underlined: text, rule.
    elif (cursor + 1 < len(lines) and lines[cursor].strip()
            and _RULE.match(lines[cursor + 1])):
        cursor += 2
    else:
        return cursor
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    return cursor


#: The keys `_docinfo` takes out of an rST document. Derived from `Options`
#: so that adding a field cannot leave it rendering as a stray table row.
_RECOGNISED = frozenset(field for field in Options.__dataclass_fields__)
