"""Colouring code blocks, in this process. pygments, behind one function.

The engine is behind a hook for the same reason the backends are: this is a
choice about appearance, and it should be replaceable without touching
anything that decides what a reader may see. Issue #9 measured tree-sitter at
6.4x pygments and found it ships no highlight queries at all, so the cheap
engine goes first and the fast one gets measured against it later.

pygments is 598 lexers of Python regexes. Two things follow, and both are
decisions rather than defaults:

**The language comes from the fence and from nowhere else.** `guess_lexer`
took 293 ms on hostile input to conclude "Text only", and a wrong guess is
worse than no colour. An unknown language is a plain block.

**One block is capped.** Neither engine is interruptible from Python, so a
cap is the only bound there is. See `MAX_LENGTH`.

The sanitiser needs no change for any of this: the output is `span` carrying
`class`, and both were already allowed. That is the whole difference from
maths (#7), which was foreign content and got dropped whole until it was
listed.

Cost, on this host: 0.10 ms for 256 B, 3.7 ms at the 4 kB cap, for ordinary
Python. A page of prose renders in 0.62 ms, so one long block is the most
expensive thing on a page -- which is what the render cache is for.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from html import escape

log = logging.getLogger(__name__)


def _limit(name: str, default: int) -> int:
    """One byte limit, from the environment, once.

    Read at import and never again, because both limits reach the renderer id
    through each backend's `options` and a backend reads its options when it is
    built. A limit that could move afterwards would change what a page holds
    without changing the key it is cached under, which is the one failure the
    id exists to prevent.

    A value that is not a number is a typo, and a typo that silently does
    nothing is worse than one that stops the program -- but not worse than a
    wiki that will not start. It warns and uses the default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %d", name, raw, default)
        return default
    if value < 0:
        log.warning("%s=%d is negative; using 0", name, value)
        return 0
    return value

#: The longest block this colours. Longer ones stay plain.
#:
#: Not a correctness limit: pygments is linear, and every pathological input
#: in issue #9 stayed linear too. It bounds the constant, which varies by two
#: orders of magnitude with what the block contains. Measured per byte:
#:
#:     ordinary Python      1.3 us      4 kB ->  3.7 ms
#:     solid punctuation   10.0 us      4 kB -> 40.0 ms
#:
#: 4 kB is eleven times the largest code block in this repository's own
#: documentation, whose median block is 158 bytes.
#: `0` turns highlighting off, which is the honest way to say so: every block
#: is then over the limit and renders plain, and the renderer id says which.
MAX_LENGTH = _limit("FSWIKI_HIGHLIGHT_BLOCK_BYTES", 4096)

#: The most a single page may colour, across all its blocks.
#:
#: MAX_LENGTH bounds one block and bounds nothing about a page, which is the
#: mistake this exists to fix. Nothing limits how many blocks a document has,
#: and `document_version.content` is a bare `text` with no size constraint, so
#: 200 blocks at the block cap is 822 kB of source and **8.7 seconds** of
#: render -- 99% of it highlighting, against 96 ms for the same page in a
#: language pygments does not know. One document, written by one user and read
#: by another. See issue #12.
#:
#: Bytes rather than time, and that is the whole design. A deadline was
#: measured and works -- the longest uninterruptible step is 0.24 ms at the
#: block cap, and the check costs nothing outside inputs with tens of thousands
#: of tokens -- but the render cache stores one body per
#: `(document_id, version, renderer)`, and nothing in that key says how busy
#: the server was. A page would come back coloured on a quiet server and plain
#: on a busy one, and whichever ran first is what every later reader gets. A
#: byte budget depends only on the content and the order of its blocks, so
#: every machine caches the same bytes.
#:
#: 32 kB is about 800 lines of code on one page, against a median block of
#: 158 B and a largest page of 14.7 kB in this repository's own documentation.
#: Measured, at 1.3 us/byte for ordinary code and 10 us/byte for the worst
#: input in issue #9: 43 ms typical, 328 ms worst.
#:
#: `0` turns highlighting off here too, and either variable is enough.
PAGE_BUDGET = _limit("FSWIKI_HIGHLIGHT_PAGE_BYTES", 32768)

#: What is left of the current page's budget, or None outside a page.
#:
#: A context variable rather than an argument because the budget belongs to the
#: page and the hooks that spend it are called by somebody else's parser, three
#: frames down, with no way to pass anything.
_remaining: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "fswiki_highlight_budget", default=None)

_highlighter: tuple = ()


@contextlib.contextmanager
def page(budget: int | None = None):
    """Give the enclosing render one page's worth of colouring.

    Outside one of these there is no budget and no accounting: a direct call to
    `to_html` is a caller asking for one block, not a page being served.
    `render.render()` opens one for every page it renders.
    """
    token = _remaining.set(PAGE_BUDGET if budget is None else budget)
    try:
        yield
    finally:
        _remaining.reset(token)


def _take(n: int) -> bool:
    """Spend `n` bytes of the page's budget, or refuse. All or nothing.

    All or nothing per block, because half a coloured block is worse than none
    and because "the first N bytes of this block" is not a thing a lexer can be
    asked for.
    """
    left = _remaining.get()
    if left is None:
        return True
    if n > left:
        return False
    _remaining.set(left - n)
    return True


def _load() -> tuple:
    """Import pygments once, and remember it if it is absent."""
    global _highlighter
    if not _highlighter:
        try:
            import pygments
            from pygments.formatters.html import HtmlFormatter
            from pygments.lexers import get_lexer_by_name
            from pygments.util import ClassNotFound
        except ImportError:
            _highlighter = (None, None, None, None, None)
        else:
            # nowrap: the spans and nothing else. The <pre><code> around them
            # is the markdown engine's, so a highlighted block and a plain one
            # are the same element with the same classes on it.
            _highlighter = (pygments.highlight, get_lexer_by_name,
                            HtmlFormatter(nowrap=True), ClassNotFound,
                            pygments.__version__)
    return _highlighter


def version() -> str | None:
    """pygments' version, or None when it is not installed.

    Every backend puts this in its options, so it reaches the renderer id and
    therefore the cache key. Without it, a page rendered with the highlighter
    present and the same page rendered without it share a key while holding
    different bytes, and a reader gets whichever was stored first.
    """
    return _load()[4]


def to_html(code: str, lang: str) -> str:
    """Colour one block. Returns the inner HTML, or "" to leave it plain.

    "" rather than an exception, and "" rather than the escaped source,
    because every caller already knows how to write a plain block and each
    engine writes its own. Never raises.
    """
    colour, get_lexer, formatter, not_found, _ = _load()
    if colour is None or not lang or len(code) > MAX_LENGTH:
        return ""
    try:
        # A lexer per call, rather than a memoised one. Measured: building it
        # is 0.046 ms against 0.525 ms of lexing, so the cache would buy 9%
        # and would share one object's state across whatever threads a
        # deployment renders on. A miss costs 0.066 ms, so a page of unknown
        # fences does not amplify either.
        lexer = get_lexer(lang)
    except not_found:
        # An unknown language is an ordinary case: somebody wrote ```notalang,
        # or a lexer this deployment does not have. A plain block is the
        # honest answer, and it is the one both engines produce anyway.
        return ""
    except Exception:
        return ""
    # Charged after the lexer is known, so a page full of ```notalang spends
    # nothing: those blocks were never going to be coloured.
    if not _take(len(code)):
        return ""
    try:
        return colour(code, lexer, formatter)
    except Exception:
        # A lexer is a few hundred regexes written by somebody else. If one of
        # them raises, this block is not coloured and the page is still a page.
        return ""


def block(code: str, lang: str) -> str:
    """A whole code block, in the shape both markdown engines already emit.

    Written once here rather than twice in `builtin`, because "both engines
    colour the same fence the same way" is the property the conformance suite
    checks and this is the cheapest way to make it true by construction.
    """
    inner = to_html(code, lang) or escape(code, quote=False)
    attr = f' class="language-{escape(lang)}"' if lang else ""
    return f"<pre><code{attr}>{inner}</code></pre>"
