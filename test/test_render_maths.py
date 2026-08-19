"""Maths, and why it needs no sandbox.

The whole point of `latex2mathml` is that it converts rather than interprets:
it reads the notation and writes MathML, and it has no filesystem and no
subprocess to reach for. So the tests that matter here are of two kinds.

First, that the two things a real TeX run would hand an author do nothing —
`\\input` reads no file, `\\write18` runs no command. Those are asserted rather
than argued, because "the library cannot do that" is the claim on which the
decision not to build a sandbox rests. Issue #7 records what the sandbox would
have cost.

Second, that MathML survives the sanitiser *and stays inert*. MathML is
foreign content to the HTML parser, so an element the sanitiser does not know
is dropped whole rather than unwrapped — which is why this needed an explicit
allowlist, and why a mistake in that allowlist is silent in one direction and
dangerous in the other.
"""

from __future__ import annotations

import pytest

from fswiki_core import render
from fswiki_core.render import maths, safety

MARKDOWN = [b.name for b in render.available() if "text/markdown" in b.content_types]
RST = "text/x-rst"
HAS_RST = any(RST in b.content_types for b in render.available())

pytestmark = pytest.mark.skipif(not MARKDOWN, reason="no markdown backend installed")


@pytest.fixture(params=MARKDOWN, ids=MARKDOWN)
def backend(request):
    """Every case runs once per engine. The two parse `$` differently and are
    required to emit the same thing."""
    return request.param


def r(text: str, backend: str) -> str:
    return render.render(text, backend=backend).html


# --- it renders -------------------------------------------------------------

def test_inline_maths_becomes_mathml(backend):
    html = r(r"Euler: $e^{i\pi}+1=0$ done.", backend)
    assert "<math" in html
    assert 'display="inline"' in html
    # The notation itself, not just a wrapper: the exponent has to be there.
    assert "<msup>" in html


def test_block_maths_becomes_mathml(backend):
    html = r("$$\n\\int_0^\\infty e^{-x^2}\\,dx = \\frac{\\sqrt{\\pi}}{2}\n$$", backend)
    assert 'display="block"' in html
    assert "<mfrac>" in html and "<msqrt>" in html


def test_both_engines_agree_about_what_maths_looks_like():
    """The reason for having two engines. They differ in what they accept as a
    delimiter; they must not differ in what a reader's browser gets."""
    shapes = {name: set(_tags(r("$x^2$", name))) for name in MARKDOWN}
    assert len(set(map(frozenset, shapes.values()))) == 1, shapes


def test_currency_is_not_maths(backend):
    """`$` is a currency sign far more often than it is a delimiter. Both
    engines are configured to leave a digit beside one alone."""
    html = r("It costs $5 and then $10 more.", backend)
    assert "<math" not in html
    assert "$5" in html and "$10" in html


def test_ordinary_prose_is_untouched(backend):
    html = r("A **bold** claim with no maths at all.", backend)
    assert "<math" not in html
    assert "<strong>" in html


@pytest.mark.skipif(not HAS_RST, reason="no reStructuredText backend installed")
def test_rst_maths_becomes_mathml():
    """docutils converts `:math:` itself, so the two markup languages reach
    MathML by different routes and must both come out the far side."""
    html = render.render(
        "Inline :math:`e^{i\\pi}+1=0` here.\n", content_type=RST).html
    assert "<math" in html and "<msup>" in html


# --- what a TeX subprocess would have given away ----------------------------

def test_input_reads_no_file(backend, tmp_path):
    """`\\input{...}` is arbitrary file disclosure under a real TeX run. Here
    it is notation with a backslash in it and nothing else."""
    secret = tmp_path / "secret.tex"
    secret.write_text("SUPER-SECRET-SERVER-CONTENTS")
    html = r(f"$\\input{{{secret}}}$", backend)
    assert "SUPER-SECRET-SERVER-CONTENTS" not in html


def test_write18_runs_nothing(backend, tmp_path):
    """`\\write18{...}` is shell escape. There is no shell to escape to."""
    marker = tmp_path / "pwned"
    r(f"$\\write18{{touch {marker}}}$", backend)
    assert not marker.exists()


def test_a_recursive_definition_does_not_take_the_page_down(backend):
    """The one failure mode the converter has: `\\def\\x{\\x}\\x` exhausts the
    Python stack in 0.5 ms. It raises rather than hanging, which is exactly
    what a TeX process in a loop would not do."""
    html = r("Before. $\\def\\x{\\x}\\x$ After.", backend)
    assert "Before." in html and "After." in html


def test_a_failed_expression_shows_its_source(backend):
    html = r("$\\def\\x{\\x}\\x$", backend)
    assert 'class="math"' in html
    assert "def" in html


def test_an_over_long_expression_is_not_converted(backend):
    """Bounded amplification, not correctness: 12 us and 14 bytes of MathML per
    byte of LaTeX, so a very long expression is shown rather than converted."""
    html = r("$" + "x+" * maths.MAX_LENGTH + "x$", backend)
    assert "<math" not in html


# --- the sanitiser, which is where MathML nearly did not survive ------------

def test_mathml_survives_the_sanitiser():
    """The regression this allowlist exists for: 206 bytes in, 0 bytes out.
    Foreign content is dropped whole rather than unwrapped, so before the
    allowlist not even the numbers came through."""
    before = maths.to_mathml(r"e^{i\pi}+1=0")
    after = safety.clean(before)
    assert "<math" in after
    assert set(_tags(before)) == set(_tags(after))


def test_the_sanitiser_is_idempotent_over_mathml():
    """A sanitiser that parses and re-serialises can emit something that parses
    differently the second time. That is the whole family of mutation-XSS bugs,
    and foreign content is where it lives."""
    once = safety.clean(maths.to_mathml(r"\frac{\sqrt{\pi}}{2}", block=True))
    assert safety.clean(once) == once


def test_href_never_survives_inside_maths(backend):
    """`\\href{...}{...}` puts an href on an `<mrow>`, and latex2mathml writes
    it out verbatim. Maths is notation, not navigation."""
    html = r(r"$\href{javascript:alert(1)}{click}$", backend)
    assert "href=" not in html
    assert "javascript" not in html


@pytest.mark.parametrize("hostile,forbidden", [
    ("<math><mrow><script>alert(1)</script></mrow></math>", "script"),
    ('<math><annotation-xml encoding="text/html"><script>alert(1)</script>'
     "</annotation-xml></math>", "script"),
    ('<math><mtext><img src=x onerror=alert(1)></mtext></math>', "onerror"),
    ('<math><mo onclick="alert(1)">+</mo></math>', "onclick"),
    ('<math onload="alert(1)"><mi>x</mi></math>', "onload"),
    ('<math><mrow href="javascript:alert(1)">x</mrow></math>', "href"),
    ('<math><maction actiontype="statusline#javascript:alert(1)">x</maction>'
     "</math>", "actiontype"),
    ('<math><mstyle style="x:y"><mi>a</mi></mstyle></math>', "style="),
    ("<math><mtext><table><mglyph><style><![CDATA[</style>"
     "<img src=x onerror=alert(1)>", "onerror"),
])
def test_nothing_executable_survives_inside_math(hostile, forbidden):
    """Written as raw HTML on purpose. No backend can produce these — raw HTML
    is off in every one — so this asserts the sanitiser alone, which is the
    layer that has to hold if a backend ever changes its mind."""
    cleaned = safety.clean(hostile)
    assert forbidden not in cleaned
    assert "alert" not in cleaned


def test_annotation_xml_is_not_allowed():
    """The named vector. `encoding="text/html"` makes it an HTML integration
    point, which is the classic route through a sanitiser that allows MathML."""
    assert "annotation-xml" not in safety.MATHML_TAGS


def test_no_mathml_element_may_carry_href():
    assert not any("href" in attrs for attrs in safety.MATHML_ATTRIBUTES.values())


def test_every_allowed_mathml_attribute_belongs_to_an_allowed_element():
    assert set(safety.MATHML_ATTRIBUTES) <= safety.MATHML_TAGS


# --- the converter on its own -----------------------------------------------

def test_to_mathml_never_raises():
    for bad in ["", "\\", "{", "}" * 100, "\\def\\x{\\x}\\x", "\\begin{nope}"]:
        assert isinstance(maths.to_mathml(bad), str)


def test_without_the_converter_the_source_is_shown(monkeypatch):
    """A deployment that installs no converter still serves the page, with the
    LaTeX visible rather than gone."""
    monkeypatch.setattr(maths, "_converter", (None, None))
    assert maths.to_mathml("x^2") == '<tt class="math">x^2</tt>'
    assert maths.to_mathml("x^2", block=True) == '<pre class="math">x^2</pre>'
    assert maths.version() is None


def test_the_shown_source_is_escaped():
    assert maths.source("<script>") == '<tt class="math">&lt;script&gt;</tt>'


def test_the_renderer_id_moves_with_the_converter(backend):
    """Which converter is installed changes the bytes, so it has to change the
    cache key. Without it the same page rendered with and without maths shares
    a key, and a reader gets whichever was stored first."""
    from fswiki_core.render import registry
    assert registry.get(name=backend).options["maths"] == maths.version()


def _tags(html: str) -> list[str]:
    import re
    return re.findall(r"<([a-zA-Z][\w-]*)", html)
