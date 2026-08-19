"""Colouring code blocks, and the three things that must stay true.

**Nothing new reaches the reader's browser.** Unlike maths (#7), which was
foreign content and got dropped whole until the sanitiser was told about it,
highlighting emits `span` carrying `class` and both were already allowed. So
the claim to check is the opposite one: that *nothing else* comes through, and
that a `style` attribute never appears -- pygments emits those only with
`noclasses=True`, which is not the default and is not set.

**Both markdown engines colour the same fence the same way.** That is the
whole reason there are two of them. `render.highlight.block` writes the
wrapper once for both, so this is true by construction and asserted anyway.

**A block that cannot be coloured is a plain block, never an error.** An
unknown language, an absent pygments, a block over the cap and a lexer that
raises are four different reasons and one behaviour.
"""

from __future__ import annotations

import re

import pytest

from fswiki_core import render
from fswiki_core.render import highlight, safety

MARKDOWN = [b.name for b in render.available() if "text/markdown" in b.content_types]
RST = "text/x-rst"
HAS_RST = any(RST in b.content_types for b in render.available())

pytestmark = pytest.mark.skipif(not MARKDOWN, reason="no markdown backend installed")

HAS_PYGMENTS = highlight.version() is not None
needs_pygments = pytest.mark.skipif(not HAS_PYGMENTS, reason="pygments not installed")

PYTHON = 'def f(x):\n    return "a" + x  # c\n'


@pytest.fixture(params=MARKDOWN, ids=MARKDOWN)
def backend(request):
    return request.param


def r(text: str, backend: str) -> str:
    return render.render(text, backend=backend).html


def fence(code: str, lang: str = "python") -> str:
    return f"```{lang}\n{code}```\n"


def classes(html: str) -> list[str]:
    return re.findall(r'class="([^"]*)"', html)


def text_of(html: str) -> str:
    """The page with every tag taken off, which is what a reader reads."""
    return re.sub(r"<[^>]+>", "", html)


# --- the module by itself ---------------------------------------------------

def test_no_language_is_no_colour():
    """A fence with no info string names nothing, and guessing is refused."""
    assert highlight.to_html(PYTHON, "") == ""


def test_an_unknown_language_is_no_colour():
    assert highlight.to_html(PYTHON, "notalang") == ""


def test_a_language_that_is_a_path_is_no_colour():
    """The info string is written by whoever wrote the page. pygments looks a
    name up in a table, so this is a table miss rather than anything else --
    but it is the case worth stating."""
    assert highlight.to_html(PYTHON, "../../etc/passwd") == ""


@needs_pygments
def test_a_known_language_is_coloured():
    out = highlight.to_html(PYTHON, "python")
    assert '<span class="k">def</span>' in out


@needs_pygments
def test_an_alias_works_because_pygments_resolves_it():
    assert highlight.to_html(PYTHON, "py") == highlight.to_html(PYTHON, "python")


@needs_pygments
def test_a_block_over_the_cap_is_left_plain():
    """Neither engine is interruptible from Python, so the cap is the only
    bound there is. See render.highlight.MAX_LENGTH."""
    assert highlight.to_html("x = 1\n" * 4000, "python") == ""


@needs_pygments
def test_a_block_at_the_cap_is_still_coloured():
    """The cap is a limit and not an approximation of one."""
    code = "x=1;" * (highlight.MAX_LENGTH // 4)
    assert len(code) == highlight.MAX_LENGTH
    assert highlight.to_html(code, "python") != ""


def test_nothing_raises_whatever_it_is_given():
    """Every caller renders a page after this returns. There is no input for
    which the right answer is an exception."""
    for lang in ("", "python", "notalang", "text", "<script>"):
        for code in ("", "\x00\x00", "\ud800" .encode("utf-16", "surrogatepass")
                     .decode("utf-16", "surrogatepass"), "x" * 100):
            assert isinstance(highlight.to_html(code, lang), str)


def test_the_source_is_in_the_output_whether_or_not_it_is_coloured():
    """A block that could not be coloured must still be a block. Losing the
    code would be the one failure a reader cannot work around."""
    for lang in ("python", "notalang", ""):
        assert "return" in text_of(highlight.block(PYTHON, lang))


def test_the_language_reaches_the_class_and_is_escaped():
    """The info string is user input and lands in an attribute. It is inert --
    `class` is on the allowlist for `code` -- but it must not break out of the
    attribute on the way there."""
    out = highlight.block("x\n", 'py"><script>')
    assert "<script>" not in out


# --- through the engines ----------------------------------------------------

@needs_pygments
def test_a_fence_is_coloured(backend):
    assert '<span class="k">def</span>' in r(fence(PYTHON), backend)


def test_an_unknown_language_is_a_plain_block(backend):
    """```notalang degrades by itself, in both engines, with the language
    still named on the element so a stylesheet or a script could use it."""
    html = r(fence(PYTHON, "notalang"), backend)
    assert "<span" not in html
    assert 'class="language-notalang"' in html
    assert "return" in text_of(html)


def test_a_fence_with_no_language_is_a_plain_block(backend):
    html = r(fence(PYTHON, ""), backend)
    assert "<span" not in html
    assert "return" in text_of(html)


@needs_pygments
def test_both_engines_colour_the_same_fence_identically():
    """The property having two engines is for. Byte for byte, because the
    wrapper is written once for both and the tokens come from one lexer."""
    if len(MARKDOWN) < 2:
        pytest.skip("only one markdown engine installed")
        return
    rendered = {b: r(fence(PYTHON), b) for b in MARKDOWN}
    assert len(set(rendered.values())) == 1, rendered


@needs_pygments
def test_the_info_string_after_the_language_is_ignored(backend):
    """```python title=x names python in both engines, and the rest is not a
    language. The two must not disagree about which word is which."""
    assert '<span class="k">def</span>' in r(
        f"```python title=x\n{PYTHON}```\n", backend)


@needs_pygments
def test_an_indented_block_is_not_coloured(backend):
    """It names no language, so there is nothing to colour it as. Both engines
    already agree about that and this is what keeps them agreeing."""
    html = r("    def f():\n        return 1\n", backend)
    assert "<span" not in html


# --- what survives the sanitiser --------------------------------------------

@needs_pygments
def test_every_class_the_highlighter_emits_survives(backend):
    """The classes are the whole output. If the sanitiser dropped them the
    block would still render, unstyled and silently -- which is the failure an
    allowlist has and a blocklist does not."""
    html = r(fence(PYTHON), backend)
    before = classes(html)
    assert before, "nothing was coloured at all"
    assert classes(safety.clean(html)) == before


@needs_pygments
def test_no_style_attribute_is_ever_emitted(backend):
    """pygments emits inline styles only with `noclasses=True`, which is not
    the default and is not set. If that ever changes, the sanitiser drops them
    and every block goes monochrome -- so it is worth failing here instead."""
    assert "style=" not in r(fence(PYTHON), backend)


@needs_pygments
def test_a_lexer_cannot_smuggle_a_tag_through(backend):
    """The code is escaped by pygments before any span goes round it. This is
    the same claim `test_no_raw_tag_survives` makes about prose, made again
    where the escaping is somebody else's."""
    html = r(fence('x = "<script>alert(1)</script>"\n', "python"), backend)
    assert "<script" not in html
    assert "alert(1)" in text_of(html)


#: Emitted, and deliberately given no rule: these are the parts of a listing
#: that are not worth a colour. Naming them is what makes the test below able
#: to fail -- without this it would only ever say "some class has no rule",
#: which is true of these four forever.
#:
#:   p  punctuation      n   a name that is just a name
#:   w  whitespace       nx  a name in a language pygments does not classify
PLAIN = {"p", "w", "n", "nx"}


@needs_pygments
def test_the_page_stylesheet_covers_the_classes_a_page_gets(backend):
    """A class with no rule is a token in the prose colour, which is a thing
    nobody notices and nothing reports. The stylesheet is trimmed on purpose,
    so this is what says how far it was trimmed -- and it fails when a
    language emits something new rather than when the trimming was wrong."""
    from fswiki_core.pages import STYLE

    seen = set()
    for lang, code in (("python", PYTHON),
                       ("bash", "# c\nset -eu\necho \"$x\"\n"),
                       ("sql", "-- c\nselect 1 from t where a = 'b';\n"),
                       ("diff", "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n")):
        for value in classes(r(fence(code, lang), backend)):
            seen.update(value.split())
    unstyled = {c for c in seen
                if not c.startswith("language-") and f"pre .{c}" not in STYLE}
    assert not unstyled - PLAIN, f"no rule for {sorted(unstyled - PLAIN)}"


# --- reStructuredText, which highlights itself ------------------------------

@pytest.mark.skipif(not HAS_RST, reason="no reStructuredText backend installed")
@needs_pygments
def test_rst_uses_the_same_class_names():
    """docutils has its own pygments integration, and `syntax_highlight`
    decides whether its class names are the short ones pygments' own formatter
    writes. They are the same names, so the two paths need one stylesheet
    between them rather than two."""
    html = render.render(".. code:: python\n\n   def f():\n       return 1\n",
                         content_type=RST).html
    assert '<span class="k">def</span>' in html


@pytest.mark.skipif(not HAS_RST, reason="no reStructuredText backend installed")
def test_rst_with_an_unknown_language_is_still_a_page():
    """docutils raises LexerError for a language pygments does not have. It
    re-lexes with 'none' instead, because `report_level` is above 2."""
    html = render.render(".. code:: notalang\n\n   zz\n", content_type=RST).html
    assert "zz" in text_of(html)


# --- the cache key ----------------------------------------------------------

def test_the_highlighter_version_is_in_every_backend_id():
    """Two deployments, one with pygments and one without, produce different
    bytes for the same revision. If they shared a renderer id they would share
    a cache key, and a reader would get whichever was stored first.

    Every backend that can colour anything, which is every backend with
    options at all: `plain` wraps text in a <pre> and has none."""
    colouring = [b for b in render.available() if b.options]
    assert colouring, "no backend declares any options"
    for b in colouring:
        assert "highlight" in b.options, b.name
        assert b.options["highlight"]["version"] == highlight.version()


def test_both_limits_are_in_the_id_too():
    """They decide what a page comes back holding just as much as the version
    does: a block over one is plain, and so is every block after the other runs
    out. A key that does not move with them serves one deployment's answer to
    another."""
    for b in [b for b in render.available() if b.options]:
        assert b.options["highlight"]["block_bytes"] == highlight.MAX_LENGTH
        assert b.options["highlight"]["page_bytes"] == highlight.PAGE_BUDGET


# --- the page budget --------------------------------------------------------

def blocks(n: int, size: int, lang: str = "python") -> str:
    """`n` fenced blocks whose content is `size` bytes each, newline included."""
    body = "x=1;" * ((size - 1) // 4)
    body += "#" * (size - 1 - len(body))
    return "".join(f"```{lang}\n{body}\n```\n\n" for _ in range(n))


def coloured_blocks(html: str) -> list[bool]:
    """Whether each <pre> in the page came back with spans in it."""
    return ["<span" in pre for pre in html.split("<pre")[1:]]


@needs_pygments
def test_a_page_stops_colouring_when_its_budget_runs_out(backend):
    """The per-block cap bounds a block and says nothing about a page, and
    nothing bounds how many blocks a document has. 200 blocks at the cap was
    822 kB of source and 8.7 seconds of render, 99% of it highlighting. See
    issue #12."""
    over = highlight.PAGE_BUDGET // highlight.MAX_LENGTH + 2
    marks = coloured_blocks(r(blocks(over, highlight.MAX_LENGTH), backend))
    assert len(marks) == over
    assert marks[0] is True, "nothing was coloured at all"
    assert marks[-1] is False, "the budget did not stop anything"


@needs_pygments
def test_the_budget_stops_at_a_block_boundary(backend):
    """All or nothing per block. Half a coloured block is worse than none, and
    "the first N bytes of this one" is not something a lexer can be asked."""
    over = highlight.PAGE_BUDGET // highlight.MAX_LENGTH + 2
    marks = coloured_blocks(r(blocks(over, highlight.MAX_LENGTH), backend))
    # Every coloured block comes before every plain one: one switch, no gaps.
    assert marks == sorted(marks, reverse=True), marks


@needs_pygments
def test_the_same_page_renders_the_same_bytes_every_time(backend):
    """The reason this is a byte budget and not a deadline. The render cache
    holds one body per (document_id, version, renderer), and nothing in that
    key says how busy the server was -- so a rule that consults the clock would
    cache whichever answer happened to run first."""
    src = blocks(highlight.PAGE_BUDGET // highlight.MAX_LENGTH + 2,
                 highlight.MAX_LENGTH)
    assert r(src, backend) == r(src, backend)


@needs_pygments
def test_every_page_gets_its_own_budget(backend):
    """It is spent per render, not per process. Otherwise the wiki would stop
    colouring anything after the first few pages a server ever served."""
    src = blocks(highlight.PAGE_BUDGET // highlight.MAX_LENGTH + 2,
                 highlight.MAX_LENGTH)
    r(src, backend)
    assert coloured_blocks(r(blocks(1, 64), backend)) == [True]


@needs_pygments
def test_blocks_that_were_never_going_to_be_coloured_cost_nothing(backend):
    """An unknown language is charged nothing, because it buys nothing. A page
    of ```notalang must not use up the budget of the one Python block after
    it."""
    src = (blocks(highlight.PAGE_BUDGET // highlight.MAX_LENGTH + 2,
                  highlight.MAX_LENGTH, lang="notalang")
           + blocks(1, 64))
    assert coloured_blocks(r(src, backend))[-1] is True


@needs_pygments
def test_a_block_over_the_block_cap_costs_nothing_either(backend):
    """Same argument: it is refused before it is charged."""
    src = blocks(2, highlight.MAX_LENGTH + 200) + blocks(1, 64)
    marks = coloured_blocks(r(src, backend))
    assert marks[0] is False and marks[-1] is True


def test_outside_a_page_there_is_no_budget():
    """`highlight.to_html` on its own is a caller asking for one block, not a
    page being served. Only render.render() opens a page."""
    body = "x=1;" * 2000
    for _ in range(20):
        assert highlight.to_html(body[:highlight.MAX_LENGTH], "python") != "" \
            or not HAS_PYGMENTS


# --- the limits, and where they come from -----------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, 4096),          # unset
    ("1024", 1024),
    ("0", 0),              # off, which is a value rather than a special case
    ("-1", 0),             # nonsense, clamped rather than refused
    ("", 4096),            # set-but-empty, which shells produce by accident
    ("4k", 4096),          # a typo: warn and carry on
    ("lots", 4096),
])
def test_a_limit_is_read_from_the_environment(monkeypatch, raw, expected):
    """A typo that silently does nothing is worse than one that stops the
    program -- but not worse than a wiki that will not start. It warns."""
    monkeypatch.delenv("FSWIKI_TEST_LIMIT", raising=False)
    if raw is not None:
        monkeypatch.setenv("FSWIKI_TEST_LIMIT", raw)
    assert highlight._limit("FSWIKI_TEST_LIMIT", 4096) == expected


@needs_pygments
def test_a_block_limit_of_zero_turns_highlighting_off(monkeypatch):
    """The honest way to say so: every block is over the limit, so every block
    renders plain, and the renderer id says which."""
    monkeypatch.setattr(highlight, "MAX_LENGTH", 0)
    assert highlight.to_html(PYTHON, "python") == ""
    assert "return" in text_of(highlight.block(PYTHON, "python"))


@needs_pygments
def test_a_page_budget_of_zero_does_too(monkeypatch):
    """Either variable is enough on its own."""
    monkeypatch.setattr(highlight, "PAGE_BUDGET", 0)
    with highlight.page():
        assert highlight.to_html(PYTHON, "python") == ""


@needs_pygments
def test_two_limits_are_two_renderer_ids(monkeypatch):
    """The point of the whole exercise. Two deployments with different limits
    hold different bytes for the same revision, so they must not share a cache
    key -- and the key has to move by itself rather than because somebody
    remembered to move it."""
    from fswiki_core.render import builtin, registry

    monkeypatch.setattr(highlight, "MAX_LENGTH", 4096)
    a = registry.config_digest(builtin.MarkdownItBackend())
    monkeypatch.setattr(highlight, "MAX_LENGTH", 1024)
    b = registry.config_digest(builtin.MarkdownItBackend())
    monkeypatch.setattr(highlight, "PAGE_BUDGET", 1024)
    c = registry.config_digest(builtin.MarkdownItBackend())
    assert len({a, b, c}) == 3, (a, b, c)
