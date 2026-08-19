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
    # Everything that changes the output, declared so the cache key moves with
    # it. `strikethrough` is enabled to match mistune: the two markdown engines
    # disagreeing about `~~struck~~` is exactly the kind of thing having two of
    # them is supposed to surface.
    options = {"preset": "commonmark", "html": False,
               "enable": ["table", "strikethrough"]}

    def __init__(self) -> None:
        import markdown_it

        self.version = markdown_it.__version__
        # html=False is the first of the two layers described in safety.py.
        self._md = markdown_it.MarkdownIt(
            self.options["preset"], {"html": self.options["html"]})
        for extension in self.options["enable"]:
            self._md.enable(extension)

    def to_html(self, text: str) -> str:
        return self._md.render(text)


class MistuneBackend:
    """mistune: the same speed, a different dialect, useful as a second opinion.

    Registered second so it does not become the default by accident, and kept
    because it is what proves the seam is real.
    """

    name = "mistune"
    content_types = MARKDOWN
    options = {"escape": True, "plugins": ["table", "strikethrough"]}

    def __init__(self) -> None:
        import mistune

        self.version = mistune.__version__
        self._md = mistune.create_markdown(**self.options)

    def to_html(self, text: str) -> str:
        return self._md(text)


class RstBackend:
    """docutils: reStructuredText, with three of its settings held down.

    Those settings are not configuration, they are the reason this backend is
    safe to register at all, and each was measured rather than assumed:

    **`file_insertion_enabled=False`.** With docutils' defaults,
    ``.. include:: /etc/passwd`` opens that file and puts its contents in the
    rendered page. That is arbitrary server-file disclosure written by one user
    and read by another, and **the sanitiser does not stop it** -- the contents
    arrive as ordinary text nodes rather than as tags, so nh3 passes them
    straight through. Measured: with the default settings the file's contents
    survive `safety.clean()` intact.

    **`raw_enabled=False`.** ``.. raw:: html`` injects arbitrary markup.
    Layered rather than load-bearing: nh3 does strip the `<script>` this one
    produces. It is off for the same reason `html=False` is set on the markdown
    backends -- hostile input should never become tags in the first place.

    **`_disable_config=True`.** The one that is genuinely surprising. A
    `docutils.conf` in the working directory **overrides `settings_overrides`**,
    so without this a file sitting next to the server silently re-enables the
    first two. Measured: the include leaked again with both flags set to False,
    and stopped once config reading was off.

    Cost, on this host: 5.14 ms for a 536 B page, against 0.62 ms for
    markdown-it on a comparable one. reStructuredText is about eight times the
    price of markdown per page, which is a few per cent of a request that
    already spends ~70 ms talking to PostgREST.
    """

    name = "docutils"
    content_types = ("text/x-rst",)

    # The settings *are* the options: three of them are security, and all of
    # them change the output, so they are what the cache key is digested from.
    options = {
        # See the docstring. These three are security, not taste.
        "file_insertion_enabled": False,
        "raw_enabled": False,
        "_disable_config": True,
        # A wiki page with a typo in it is still a page. Never raise, and never
        # render docutils' own complaints into what the reader sees.
        "report_level": 5,
        "halt_level": 5,
        "embed_stylesheet": False,
        # Avoids a pygments dependency, and pygments emits inline style
        # attributes that the sanitiser would drop anyway.
        "syntax_highlight": "none",
    }

    def __init__(self) -> None:
        import docutils
        from docutils.core import publish_parts

        self.version = docutils.__version__
        self._publish = publish_parts

    def to_html(self, text: str) -> str:
        # `writer=` and not `writer_name=`: the latter is pending removal in
        # docutils 2.0 and warns on every call.
        return self._publish(
            text, writer="html5", settings_overrides=self.options
        )["html_body"]


class PlainTextBackend:
    """text/plain, wrapped in a <pre>. No library, so it is always available.

    Worth having beyond the obvious: it is the backend the conformance suite
    can always run against, and it is what a document whose renderer has been
    uninstalled degrades to rather than erroring.
    """

    name = "plain"
    version = "1"
    content_types = ("text/plain",)
    #: Nothing to configure, so nothing to digest: its id stays `plain/1+fswikiN`.
    options: dict = {}

    def to_html(self, text: str) -> str:
        from html import escape

        return f"<pre>{escape(text)}</pre>"


def _install() -> None:
    # Last registration wins the content type, so the preferred engine goes
    # last. Each failure is logged at debug: an absent optional dependency is
    # a deployment choice, not a fault.
    for factory in (MistuneBackend, MarkdownItBackend, RstBackend, PlainTextBackend):
        try:
            register(factory())
        except ImportError as exc:
            log.debug("render backend %s unavailable: %s", factory.__name__, exc)


_install()
