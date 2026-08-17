"""Wiki links: recognised before the backend, resolved after it.

`[[engineering/onboarding]]` is not CommonMark and no backend has to know
about it. It is rewritten into an ordinary link with an `/-/fswiki/` prefix on the
way in, and the anchors that come back out are resolved on the way to a
particular reader. Both halves live here so that every backend behaves the
same, including ones written later by somebody else.

Resolution is deliberately a separate step from rendering, because the two have
different lifetimes. The rendered body is a function of the revision and can be
cached forever; which links a reader may follow is a function of the ACL and
has to be decided per request. Baking liveness into the body would make it
per-reader and there would be nothing left to cache.

**A link the reader may not follow renders as plain text, and as exactly the
same plain text as a link to a document that does not exist.** A live link to
something you may not read discloses that it exists, where it lives and what it
is called, which is three things the ACL did not grant — and it discloses them
in the HTML, before any click could reach the audit trail. "Forbidden" and
"missing" have to be indistinguishable, or the difference between them is the
disclosure.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Callable

from .. import naming

# A reserved path prefix, not a URL scheme.
#
# `fswiki:` was the obvious choice and it does not survive contact with a
# second backend: mistune allowlists link schemes — for the same reason we do —
# and rewrites anything unfamiliar to `#harmful-link`, so the target vanished
# before the post-pass could see it. The conformance suite caught it on the
# first run, which is the entire argument for having one.
#
# A relative path under a prefix we own is accepted by every engine, because it
# is an ordinary link. `/-/` is reserved: no document path can produce it, since
# a slug may not be empty and may not contain a slash.
PREFIX = "/-/fswiki/"

# [[path]] or [[path|label]]. The path is a display path (a/b) or an ltree
# path (root.a.b); naming.from_display settles which. Deliberately not matching
# across newlines: an unclosed bracket should stay literal text rather than
# swallowing the rest of the page.
_WIKILINK = re.compile(r"\[\[([^\[\]|\n]+)(?:\|([^\[\]\n]*))?\]\]")


def expand(text: str, *, to_path: Callable[[str], str] | None = None) -> str:
    """Rewrite [[wikilinks]] into ordinary links the backend understands.

    The target becomes `/-/fswiki/<ltree path>`. Unresolved, it points at a
    prefix that belongs to us and to nothing else, so a body served without
    being composed produces a request we can answer with a permission check
    rather than a leak.
    """
    resolve_path = to_path or _default_path

    def one(match: re.Match[str]) -> str:
        target, label = match.group(1).strip(), match.group(2)
        if not target:
            return match.group(0)
        try:
            path = resolve_path(target)
        except ValueError:
            # Not a representable path. Leave the source alone rather than
            # inventing a link to somewhere that cannot exist.
            return match.group(0)
        shown = (label if label is not None else target).strip() or target
        # Escape for link-destination syntax, not for HTML: the backend still
        # has to parse this as markdown.
        return f"[{_escape_label(shown)}]({PREFIX}{path})"

    return _WIKILINK.sub(one, text)


def _default_path(target: str) -> str:
    """Interpret a wikilink target as an ltree path."""
    if target.startswith("root.") or target == "root":
        return target
    return naming.from_display(target)


def _escape_label(text: str) -> str:
    return text.replace("[", r"\[").replace("]", r"\]")


class _Resolver(HTMLParser):
    """Rewrite `fswiki:` anchors according to what one reader may see."""

    def __init__(self, allow: Callable[[str], str | None]) -> None:
        super().__init__(convert_charrefs=False)
        self._allow = allow
        self.out: list[str] = []
        # Depth of anchors we are dropping, so nested markup inside a
        # forbidden link still has its text kept.
        self._dropping = 0
        self.unresolved = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            values = dict(attrs)
            href = values.get("href") or ""
            if href.startswith(PREFIX):
                url = self._allow(href[len(PREFIX):])
                if url is None:
                    # Not a broken link, not a live one: no link at all.
                    self._dropping += 1
                    return
                values["href"] = url
                self.out.append(_tag("a", values))
                return
        self.out.append(_tag(tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.out.append(_tag(tag, dict(attrs), self_closing=True))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._dropping:
            self._dropping -= 1
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:  # pragma: no cover - stripped
        pass


def _tag(name: str, attrs: dict[str, str | None], *, self_closing: bool = False) -> str:
    parts = [name]
    for key, value in attrs.items():
        if value is None:
            parts.append(key)
        else:
            parts.append(f'{key}="{html.escape(value, quote=True)}"')
    return f"<{' '.join(parts)}{' /' if self_closing else ''}>"


def resolve(rendered: str, allow: Callable[[str], str | None]) -> str:
    """Turn wiki anchors into real links, or into plain text.

    `allow` is given a document path and returns the URL this reader should
    follow, or None for "they may not know this exists". Returning None for a
    path that is merely absent is not an accident — see the module docstring.
    """
    resolver = _Resolver(allow)
    resolver.feed(rendered)
    resolver.close()
    return "".join(resolver.out)


def unresolved(rendered: str) -> int:
    """How many wiki anchors are still unresolved.

    A composed page should contain none. Serving a cached body without
    resolving it is the mistake this exists to catch, because the result looks
    almost right: the links are inert, but the paths are in the DOM.
    """
    return rendered.count(f'href="{PREFIX}')
