"""Markup to HTML, with the backend chosen rather than assumed.

    >>> from fswiki_core import render
    >>> page = render.render("# Hi\n\nsee [[public/welcome]]")
    >>> page.renderer
    'markdown-it-py/4.2.0+fswiki1'

The pipeline is three steps and only the middle one is pluggable:

    [[wikilinks]] -> backend.to_html() -> sanitise

`page.renderer` identifies the whole pipeline, not just the backend, and it
belongs in any cache key alongside `(document_id, version)`. Those three name
one byte string forever, because a revision's content never changes — which is
what makes rendering cacheable without an invalidation story. Leave the
renderer out of the key and an engine upgrade quietly serves output the current
code would not produce.

Links come back under a reserved `/-/fswiki/` prefix and are resolved per
reader by
`render.links.resolve`, which is a separate step because a rendered body is
shared and a reader's permissions are not. See docs/rendering.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import builtin as _builtin  # noqa: F401 - registers the shipped backends
from . import links, safety
from .registry import (
    PIPELINE_VERSION,
    Backend,
    UnknownBackend,
    available,
    get,
    register,
)

__all__ = [
    "Backend", "Rendered", "UnknownBackend", "available", "get", "links",
    "register", "render", "safety",
]


@dataclass(frozen=True)
class Rendered:
    """HTML, and enough about how it was made to cache it safely."""

    html: str
    #: `<backend>/<version>+fswiki<pipeline>`. Part of the cache key.
    renderer: str
    content_type: str

    @property
    def unresolved_links(self) -> int:
        """Wiki anchors not yet resolved. A composed page has none."""
        return links.unresolved(self.html)


def render(text: str, *, content_type: str = "text/markdown",
           backend: str | None = None) -> Rendered:
    """Render `text`, sanitised, with wiki links left for the reader's pass.

    `backend` names one explicitly; otherwise $FSWIKI_RENDERER, otherwise the
    preferred backend for the content type.
    """
    chosen = get(content_type, backend)
    html = safety.clean(chosen.to_html(links.expand(text)))
    return Rendered(
        html=html,
        renderer=f"{chosen.name}/{chosen.version}+fswiki{PIPELINE_VERSION}",
        content_type=content_type,
    )
