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

from . import highlight, maths
from .registry import register

log = logging.getLogger(__name__)

MARKDOWN = ("text/markdown",)


def _convert(latex: str, options: dict) -> str:
    """The maths hook mdit-py-plugins takes, which by default only escapes."""
    return maths.to_mathml(latex, block=options["display_mode"])


def _language(info: str | None) -> str:
    """The language named by a fence, which is the first word of its info.

    ```` ```python title=x ```` names python. Both engines already agree on
    that, so this only has to agree with them.
    """
    return info.strip().split(maxsplit=1)[0] if info and info.strip() else ""


def _highlighting() -> dict:
    """What the highlighter will do, as data, for the renderer id.

    The version alone is not enough. Both limits decide what a page comes back
    holding -- a block over one is plain, and so is every block after the other
    runs out -- so a change to either has to move the cache key with it, the
    same argument that put the version here.
    """
    return {"version": highlight.version(),
            "block_bytes": highlight.MAX_LENGTH,
            "page_bytes": highlight.PAGE_BUDGET}


def _highlight(code: str, lang: str, attrs: str) -> str:
    """The hook markdown-it takes. A return starting with `<pre` is used as
    it stands, which is what lets one function serve both engines."""
    return highlight.block(code, lang)


def _inline_math(renderer, latex: str) -> str:
    return f'<span class="math inline">{maths.to_mathml(latex)}</span>'


def _block_math(renderer, latex: str) -> str:
    return f'<div class="math block">{maths.to_mathml(latex, block=True)}</div>\n'


def _highlighting_renderer(mistune, escape: bool):
    """mistune's HTML renderer, with fenced blocks coloured.

    A subclass rather than `renderer.register("block_code", ...)`, which is
    how the maths hooks are installed a few lines below. The difference is
    real: mistune looks a token type up as an attribute first and only falls
    back to what was registered, so registering over a method that already
    exists does nothing at all. `inline_math` is not a method; `block_code`
    is.

    Defined here rather than at module level because mistune is an optional
    dependency, and a class statement cannot wait for an import that may
    never happen.
    """

    class HighlightingRenderer(mistune.HTMLRenderer):
        def block_code(self, code: str, info: str | None = None) -> str:
            # The trailing newline is mistune's own convention between blocks.
            return highlight.block(code, _language(info)) + "\n"

    return HighlightingRenderer(escape=escape)


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
               "enable": ["table", "strikethrough"],
               # `$` is a currency sign far more often than it is a maths
               # delimiter, so the plugin has to be told when it is not maths.
               # `allow_digits` off leaves "$5 and $10" alone and `allow_space`
               # off leaves "$ 5" alone. mistune's own pattern refuses both
               # already, so this is how the two engines are made to agree.
               # Nested here rather than beside the class so that flipping one
               # moves the cache key, which is what `options` is for.
               "dollarmath": {"allow_labels": False, "allow_space": False,
                              "allow_digits": False, "double_inline": False}}

    def __init__(self) -> None:
        import markdown_it
        from mdit_py_plugins.dollarmath import dollarmath_plugin

        self.version = markdown_it.__version__
        # html=False is the first of the two layers described in safety.py.
        self._md = markdown_it.MarkdownIt(
            self.options["preset"],
            {"html": self.options["html"], "highlight": _highlight})
        for extension in self.options["enable"]:
            self._md.enable(extension)
        self._md.use(dollarmath_plugin, renderer=_convert,
                     **self.options["dollarmath"])
        # Which maths converter and which highlighter are installed change
        # the bytes, so they go in the id beside the options that produced
        # them. Both are None when the library is absent, which is a distinct
        # key rather than the same one holding different output.
        self.options = self.options | {"maths": maths.version(),
                                       "highlight": _highlighting()}

    def to_html(self, text: str) -> str:
        return self._md.render(text)


class MistuneBackend:
    """mistune: the same speed, a different dialect, useful as a second opinion.

    Registered second so it does not become the default by accident, and kept
    because it is what proves the seam is real.
    """

    name = "mistune"
    content_types = MARKDOWN
    options = {"escape": True, "plugins": ["table", "strikethrough", "math"]}

    def __init__(self) -> None:
        import mistune

        self.version = mistune.__version__
        self._md = mistune.create_markdown(
            renderer=_highlighting_renderer(mistune, self.options["escape"]),
            plugins=self.options["plugins"])
        # mistune's math plugin parses `$...$` and then writes the LaTeX back
        # out untouched. These two convert it instead, in the wrapper
        # mdit-py-plugins uses, so the engines differ in what they accept and
        # not in what they emit.
        self._md.renderer.register("inline_math", _inline_math)
        self._md.renderer.register("block_math", _block_math)
        self.options = self.options | {"maths": maths.version(),
                                       "highlight": _highlighting()}

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
        # `short` and not `long`, because the short names are the ones
        # pygments' own HTML formatter emits -- so the markdown path and this
        # one need one stylesheet between them rather than two. Measured on
        # docutils 0.23 with pygments 2.20: zero inline style attributes, and
        # every class comes through `safety.clean()` intact, because `span`
        # and `class` were already allowed.
        #
        # It degrades by itself. docutils raises LexerError when pygments is
        # absent or the language is unknown, and re-lexes with 'none' when
        # `report_level` is above 2 -- which it is, two lines up. So this
        # setting is safe to state unconditionally, and `highlight.version()`
        # in the options below is what moves the cache key when the answer
        # changes. See render.highlight and issue #9.
        "syntax_highlight": "short",
        # docutils converts `:math:` to MathML itself, so this backend needs
        # no latex2mathml. Named rather than left to the default because the
        # default has moved: older docutils wrote HTML with a stylesheet, and
        # an upgrade must not quietly change what a page contains.
        "math_output": "mathml",
    }

    def __init__(self) -> None:
        import docutils
        from docutils.core import publish_parts

        self.version = docutils.__version__
        self._publish = publish_parts
        self.options = self.options | {"highlight": _highlighting()}

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
