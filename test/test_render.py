"""What every render backend must do, whichever one it is.

A plugin seam is a promise about behaviour, and a promise nobody checks is a
comment. So these run against **every registered backend** and fail if any of
them disagrees where the wiki depends on agreement.

They deliberately do not compare HTML byte for byte. Engines differ on
whitespace, attribute order, and whether a paragraph wraps a lone image, and
none of that matters.

No stack, no network, no mount: pure functions, so these are the tests worth
running first when something is broken.
"""

from __future__ import annotations

import pytest

from fswiki_core import naming, render
from fswiki_core.render import links, registry

MARKDOWN = [b.name for b in render.available() if "text/markdown" in b.content_types]

# A suite that silently tests nothing is worse than one that fails.
pytestmark = pytest.mark.skipif(not MARKDOWN, reason="no markdown backend installed")


@pytest.fixture(params=MARKDOWN, ids=MARKDOWN)
def backend(request):
    """Every case below runs once per installed engine.

    That is the whole argument for having two: a seam with one implementation
    is not a seam, it is an abstraction that has never met a second case and
    therefore still encodes the first one's assumptions.
    """
    return request.param


def r(text: str, backend: str) -> str:
    return render.render(text, backend=backend).html


# ---------------------------------------------------------------------------
# A document is written by one user and read by another
# ---------------------------------------------------------------------------

def test_no_script_tag_survives(backend):
    assert "<script" not in r("<script>alert(1)</script>", backend)


def test_no_raw_tag_survives(backend):
    # The tag itself must never materialise. Both engines escape it into
    # visible text, where the word "onclick" is harmless — searching for the
    # word would fail this check for the wrong reason.
    assert "<div" not in r("<div onclick='x'>hi</div>", backend)


def test_no_javascript_href_survives(backend):
    # On the href, not on the string: markdown-it declines to build the link at
    # all and leaves the source as visible text, which is safe and arguably
    # clearer. Searching for the word would call that a failure.
    html = r("[click](javascript:alert(1))", backend).replace("&#", "")
    assert 'href="javascript' not in html


# ---------------------------------------------------------------------------
# Wiki links, which no backend knows about — the pre-pass does
# ---------------------------------------------------------------------------

def test_wikilink_becomes_a_reserved_prefix_anchor(backend):
    html = r("see [[public/welcome]]", backend)
    assert f'href="{links.PREFIX}root.public.welcome"' in html
    assert links.unresolved(html) == 1


def test_an_allowed_link_becomes_a_real_href(backend):
    resolved = links.resolve(r("[[public/welcome]]", backend), lambda p: f"/w/{p}")
    assert 'href="/w/root.public.welcome"' in resolved
    assert links.unresolved(resolved) == 0


def test_forbidden_and_missing_are_indistinguishable(backend):
    """The property the whole link-leak argument rests on.

    If these two ever differ, a page you may read tells you that a page you may
    not read exists — and the ACL granted none of that.
    """
    forbidden = links.resolve(r("[[secret/plans|Label]]", backend), lambda p: None)
    missing = links.resolve(r("[[gone/away|Label]]", backend), lambda p: None)
    assert forbidden == missing


def test_a_refused_link_keeps_its_text_but_is_not_a_link(backend):
    out = links.resolve(r("[[secret/plans|Label]]", backend), lambda p: None)
    assert "<a" not in out
    assert "Label" in out


# ---------------------------------------------------------------------------
# Still a markdown engine, or it is not a backend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fragment", ["<h1", "<strong", "<ul", "<li"])
def test_ordinary_markdown_still_works(backend, fragment):
    assert fragment in r("# Title\n\ntext with **bold**\n\n- one\n- two", backend)


# ---------------------------------------------------------------------------
# The renderer id is a cache key
# ---------------------------------------------------------------------------

def test_renderer_id_names_the_backend_and_version(backend):
    page = render.render("x", backend=backend)
    version = next(b.version for b in render.available() if b.name == backend)
    assert page.renderer.startswith(f"{backend}/{version}")
    # The pre- and post-passes affect the bytes too, so they are in the key.
    assert f"+fswiki{registry.PIPELINE_VERSION}" in page.renderer


def test_every_backend_produces_a_distinct_id():
    """Leave this out and switching engines serves output nobody would produce."""
    ids = {render.render("x", backend=name).renderer for name in MARKDOWN}
    assert len(ids) == len(MARKDOWN)


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------

def test_an_unknown_backend_is_refused():
    with pytest.raises(render.UnknownBackend):
        render.render("x", backend="no-such-engine")


def test_an_unhandled_content_type_is_refused():
    with pytest.raises(render.UnknownBackend):
        render.render("x", content_type="application/x-nonsense")


def test_plain_text_goes_to_its_own_backend():
    plain = render.render("a < b", content_type="text/plain")
    assert plain.renderer.startswith("plain/")
    assert "&lt;" in plain.html          # escaped, not interpreted


# ---------------------------------------------------------------------------
# Paths, which the renderer and the CLI both have to agree about
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("display,ltree", [
    ("public/welcome", "root.public.welcome"),
    ("public/welcome.md", "root.public.welcome"),
    ("root/public/welcome", "root.public.welcome"),
    ("engineering/guides/onboarding", "root.engineering.guides.onboarding"),
])
def test_from_display(display, ltree):
    assert naming.from_display(display) == ltree


@pytest.mark.parametrize("bad", ["..", "x/..", ".hidden", "a.b.c", "has space"])
def test_from_display_refuses_what_the_wiki_cannot_hold(bad):
    """A link to a name the wiki could never hold stays literal text.

    Coercing it into something almost right is the failure worth avoiding:
    `[[a.b.c]]` is a filename, not a path, and quietly turning it into one
    would invent a document.
    """
    with pytest.raises(ValueError):
        naming.from_display(bad)


@pytest.mark.parametrize("lenient,expect", [
    ("", "root"),            # [[/]] is the root, not an error
    ("/", "root"),
    ("a//b", "root.a.b"),    # empty separators collapse, as in a real path
])
def test_from_display_is_lenient_about_separators(lenient, expect):
    assert naming.from_display(lenient) == expect
