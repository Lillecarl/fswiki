"""Sanitising. Runs over every backend's output, always.

Not pluggable, on purpose. A backend is chosen for how it reads markup; this
decides what a reader's browser is allowed to execute, and those are not the
same kind of decision. Letting a backend opt out of sanitising would make the
safety of the wiki depend on which engine an operator picked, which is exactly
the coupling the plugin seam exists to avoid.

It is also cheap enough not to argue about: 0.59 ms on a 5.4 kB page, against
0.6 ms per kB to render it in the first place.

Two layers, because a parser and a sanitiser fail differently. Backends are
asked to disable raw HTML, so hostile markup never becomes tags at all; this
catches what the parser was never asked about — a `javascript:` href, an
`onclick`, a plugin that emits something unexpected.
"""

from __future__ import annotations

# Enough for prose. Anything not listed is dropped rather than escaped, so a
# stray tag disappears instead of being shown to the reader as source.
TAGS = {
    "p", "br", "hr", "em", "strong", "del", "ins", "sub", "sup", "code", "pre",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "a", "img", "span", "div",
}

ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    # Heading ids are how a deep link into a page works at all.
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"},
    "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
    "th": {"align"}, "td": {"align"},
    "code": {"class"}, "pre": {"class"}, "span": {"class"}, "div": {"class"},
    "ol": {"start"},
}

# Wiki links are relative paths under a reserved prefix rather than a custom
# scheme, so nothing has to be allowlisted for them — see render.links. `data:`
# is absent deliberately: it is how an image becomes a script in some browsers.
URL_SCHEMES = {"http", "https", "mailto"}


class SanitiserUnavailable(RuntimeError):
    """nh3 is not installed, and rendering without it is not on offer."""


def clean(html: str) -> str:
    """Strip anything a reader's browser should not be given."""
    try:
        import nh3
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise SanitiserUnavailable(
            "nh3 is required to render: serving unsanitised HTML written by "
            "one user to another user's browser is not a supported mode"
        ) from exc

    return nh3.clean(
        html,
        tags=TAGS,
        attributes=ATTRIBUTES,
        url_schemes=URL_SCHEMES,
        strip_comments=True,
        link_rel="noopener noreferrer",
    )
