"""The backends that ship with fswiki.

Each registers itself only if its library imports, so nothing here is a hard
dependency of `fswiki-core`. A deployment that wants one engine installs one
engine; the others are absent rather than broken, and `available()` says which
is which.

Two markdown backends rather than one, deliberately. A plugin seam with a
single implementation is not a seam — it is an abstraction nobody has tested
against a second case, and it will turn out to encode the first case's
assumptions. The conformance suite runs against every registered backend for
the same reason.
"""

from __future__ import annotations

import logging

from .registry import register

log = logging.getLogger(__name__)

MARKDOWN = ("text/markdown",)


class MarkdownItBackend:
    """markdown-it-py: CommonMark, which is a specification rather than a dialect.

    The default where it is available. The filesystem is the source of truth
    here and files get edited by tools that know nothing about us, so a
    document should mean the same thing to all of them.
    """

    name = "markdown-it-py"
    content_types = MARKDOWN

    def __init__(self) -> None:
        import markdown_it

        self.version = markdown_it.__version__
        # html=False is the first of the two layers described in safety.py.
        self._md = markdown_it.MarkdownIt("commonmark", {"html": False})
        self._md.enable("table")

    def to_html(self, text: str) -> str:
        return self._md.render(text)


class MistuneBackend:
    """mistune: the same speed, a different dialect, useful as a second opinion.

    Registered second so it does not become the default by accident, and kept
    because it is what proves the seam is real.
    """

    name = "mistune"
    content_types = MARKDOWN

    def __init__(self) -> None:
        import mistune

        self.version = mistune.__version__
        self._md = mistune.create_markdown(
            escape=True, plugins=["table", "strikethrough"])

    def to_html(self, text: str) -> str:
        return self._md(text)


class PlainTextBackend:
    """text/plain, wrapped in a <pre>. No library, so it is always available.

    Worth having beyond the obvious: it is the backend the conformance suite
    can always run against, and it is what a document whose renderer has been
    uninstalled degrades to rather than erroring.
    """

    name = "plain"
    version = "1"
    content_types = ("text/plain",)

    def to_html(self, text: str) -> str:
        from html import escape

        return f"<pre>{escape(text)}</pre>"


def _install() -> None:
    # Last registration wins the content type, so the preferred engine goes
    # last. Each failure is logged at debug: an absent optional dependency is
    # a deployment choice, not a fault.
    for factory in (MistuneBackend, MarkdownItBackend, PlainTextBackend):
        try:
            register(factory())
        except ImportError as exc:
            log.debug("render backend %s unavailable: %s", factory.__name__, exc)


_install()
