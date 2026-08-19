"""Markup to HTML, with the backend chosen rather than assumed.

    >>> from fswiki_core import render
    >>> page = render.render("# Hi\n\nsee [[public/welcome]]")
    >>> page.renderer
    'markdown-it-py/4.2.0+cfg91b4fb2e+fswiki4'

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
from . import cache, highlight, links, safety
from .registry import (
    PIPELINE_VERSION,
    Backend,
    UnknownBackend,
    available,
    config_digest,
    get,
    register,
)

__all__ = [
    "Backend", "Rendered", "UnknownBackend", "available", "cache", "get",
    "highlight", "links", "register", "render", "renderer_id", "safety",
]


@dataclass(frozen=True)
class Rendered:
    """HTML, and enough about how it was made to cache it safely."""

    html: str
    #: `<backend>/<version>+cfg<options>+fswiki<pipeline>`. The cache key.
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
    # One page, one colouring budget. Without it nothing bounds a document:
    # the per-block cap says nothing about how many blocks there are, and 200
    # of them at the cap is 8.7 seconds. See render.highlight.PAGE_BUDGET.
    with highlight.page():
        html = safety.clean(chosen.to_html(links.expand(text)))
    return Rendered(
        html=html,
        renderer=_renderer_id(chosen),
        content_type=content_type,
    )


def renderer_id(content_type: str = "text/markdown",
                backend: str | None = None) -> str:
    """What `render()` would stamp on the output, without rendering anything.

    The cache key needs it before there is anything to render. Raises the same
    `UnknownBackend` that `render()` would, so a caller that asks for the key
    first learns about a missing engine at the same point either way.
    """
    return _renderer_id(get(content_type, backend))


def _renderer_id(backend) -> str:
    """What produced this HTML, precisely enough to key a cache on.

    Three parts, because there are three things that can change the bytes: the
    engine, how it was configured, and the passes on either side of it. Leave
    any of them out and switching or reconfiguring quietly serves output the
    running code would not produce.
    """
    digest = config_digest(backend)
    config = f"+cfg{digest}" if digest else ""
    return f"{backend.name}/{backend.version}{config}+fswiki{PIPELINE_VERSION}"
