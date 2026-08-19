"""LaTeX maths to MathML, in this process. No TeX, no subprocess, no sandbox.

`latex2mathml` is a converter rather than an interpreter. It reads the
notation and writes MathML, and it has no filesystem and no subprocess to
reach for. That is the whole safety argument, and it was measured against the
two things a real TeX run would hand an author:

    \\input{/etc/hostname}      260 B of MathML; the hostname is not in it
    \\write18{touch /tmp/x}     291 B of MathML; nothing runs

So none of what compiling a document needs is needed here -- no scratch
directory, no rlimits, no wall-clock timeout, no concurrency cap. Issue #7
records what the other route would cost.

What it does need is a fallback, because three things can go wrong and a page
with a mistake in it is still a page:

* the converter is not installed, which is a deployment choice;
* the expression is malformed, which is an author's typo;
* the expression is pathological. ``\\def\\x{\\x}\\x`` exhausts the Python
  stack in 0.5 ms and raises `RecursionError`. It is catchable, which a TeX
  process in a loop would not be.

All three show the LaTeX source instead, in the shape docutils already uses
for maths it cannot convert. So the markdown path and the reStructuredText
path degrade the same way.

Cost: 0.32 ms for ``e^{i\\pi}+1=0``.
"""

from __future__ import annotations

from html import escape

#: The longest expression this converts. Longer ones show their source.
#:
#: Not a correctness limit. The converter is linear and does not hang; this
#: bounds amplification. Measured at 12 us and 14 bytes of MathML per byte of
#: LaTeX, so 100 kB of ``x+x+x...`` costs 1.2 s and produces 1.4 MB. Real
#: expressions are tens of bytes.
MAX_LENGTH = 4096

_converter: tuple = ()


def _load() -> tuple:
    """Import the converter once, and remember it if it is absent."""
    global _converter
    if not _converter:
        try:
            import latex2mathml
            from latex2mathml.converter import convert
        except ImportError:
            _converter = (None, None)
        else:
            _converter = (convert, latex2mathml.__version__)
    return _converter


def version() -> str | None:
    """The converter's version, or None when it is not installed.

    Both markdown backends put this in their options, so it reaches the
    renderer id and therefore the cache key. Without it, one page rendered
    with the converter present and the same page rendered with it absent share
    a key while holding different bytes, and a reader gets whichever was
    stored first.
    """
    return _load()[1]


def to_mathml(latex: str, *, block: bool = False) -> str:
    """Convert one expression. Never raises: on any failure, show the source."""
    convert, _ = _load()
    if convert is not None and len(latex) <= MAX_LENGTH:
        try:
            return convert(latex, display="block" if block else "inline")
        except Exception:
            # Every failure looks the same to a reader: this expression did
            # not become maths. Which one it was does not change what the page
            # should do, and none of them is worth losing the page for.
            pass
    return source(latex, block=block)


def source(latex: str, *, block: bool = False) -> str:
    """The expression as written, in the shape docutils uses for the same case."""
    tag = "pre" if block else "tt"
    return f'<{tag} class="math">{escape(latex)}</{tag}>'
