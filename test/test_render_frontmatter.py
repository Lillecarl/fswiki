"""Frontmatter: what a document may say about its shell, and what it may not.

Two halves, and the second is the point of the first.

The parser half is ordinary: a `---` block sets `layout`, an unknown key does
nothing, and anything that is not frontmatter is left alone. The rule worth
stating is that the *text* must survive every one of those cases. A page whose
first paragraph disappeared because it happened to sit between two horizontal
rules would be a far worse bug than a `layout:` nobody honoured.

The other half is `test_render_frontmatter_cannot_reach_the_shell`, below.
Frontmatter is written by one user and shapes the page a different user reads,
and `render.safety` does not cover it -- the sanitiser cleans the body, and the
shell is composed afterwards. So the assertions there are about what a hostile
document *cannot* do, and the impersonation banner is the one that matters
most: a page that could hide "viewing as X" could make somebody else's wiki
look like your own.
"""

from __future__ import annotations

import pytest

from fswiki_core import render
from fswiki_core.pages import Pages
from fswiki_core.render import frontmatter

RST = "text/x-rst"


def split(text, content_type="text/markdown"):
    return frontmatter.split(text, content_type)


# ---------------------------------------------------------------------------
# The block itself
# ---------------------------------------------------------------------------

def test_a_block_sets_the_layout_and_leaves_the_body():
    options, body = split("---\nlayout: wide\n---\n# Hi\n")
    assert options.layout == "wide"
    assert body == "# Hi\n"


def test_a_document_with_no_block_is_untouched():
    text = "# Hi\n\nsome prose\n"
    options, body = split(text)
    assert options == frontmatter.Options()
    assert body == text


def test_the_default_layout_is_default():
    assert frontmatter.Options().layout == "default"
    assert "default" in frontmatter.LAYOUTS


def test_an_unknown_key_does_nothing():
    options, body = split("---\nlayout: wide\nauthor: bob\n---\nbody\n")
    assert options.layout == "wide"
    assert body == "body\n"
    # And it is not smuggled in under another name either.
    assert "bob" not in repr(options)


def test_an_unknown_layout_falls_back_rather_than_being_used():
    options, _ = split("---\nlayout: sidebar-with-ads\n---\nbody\n")
    assert options.layout == "default"


def test_the_layout_value_is_matched_case_insensitively():
    assert split("---\nlayout: WIDE\n---\n")[0].layout == "wide"
    assert split("---\nLAYOUT: wide\n---\n")[0].layout == "wide"


def test_quotes_come_off_a_value():
    assert split('---\nlayout: "wide"\n---\n')[0].layout == "wide"
    assert split("---\nlayout: 'wide'\n---\n")[0].layout == "wide"


def test_blank_lines_and_comments_are_allowed_inside():
    options, body = split("---\n# what this page is\n\nlayout: wide\n---\nbody\n")
    assert options.layout == "wide"
    assert body == "body\n"


def test_the_alternative_closing_fence_works():
    options, body = split("---\nlayout: wide\n...\nbody\n")
    assert options.layout == "wide"
    assert body == "body\n"


def test_an_empty_block_is_still_a_block():
    options, body = split("---\n---\nbody\n")
    assert options.layout == "default"
    assert body == "body\n"


# ---------------------------------------------------------------------------
# Nothing here may cost a reader their content
# ---------------------------------------------------------------------------

def test_a_horizontal_rule_is_not_frontmatter():
    # This is the case that decides the all-or-nothing rule. It opens exactly
    # like a block and it is a rule, a paragraph and another rule.
    text = "---\n\nSomething important.\n\n---\n\nMore.\n"
    options, body = split(text)
    assert options.layout == "default"
    assert body == text


def test_an_unterminated_block_keeps_the_document():
    text = "---\nlayout: wide\n\nand then the document just goes on\n"
    options, body = split(text)
    assert options.layout == "default"
    assert body == text


def test_a_malformed_block_keeps_the_document():
    text = "---\nlayout: wide\n  - this is a list, which we do not parse\n---\nbody\n"
    options, body = split(text)
    assert options.layout == "default"
    assert body == text


def test_a_block_that_is_not_at_the_top_is_not_frontmatter():
    text = "# Title\n\n---\nlayout: wide\n---\n"
    options, body = split(text)
    assert options.layout == "default"
    assert body == text


@pytest.mark.parametrize("text", [
    "", "\n", "---", "---\n", "-", "--\n--\n", "---\n\n---\n",
    "---\n:\n---\n", "---\nlayout\n---\n", "---\nlayout:\n---\n",
    "\ufeff---\nlayout: wide\n---\n",
])
def test_nothing_raises_and_nothing_vanishes(text):
    """A wiki page with a typo in it is still a page."""
    options, body = split(text)
    assert isinstance(options, frontmatter.Options)
    # Either the block was recognised and removed, or the text came back whole.
    assert body == text or text.endswith(body)


# ---------------------------------------------------------------------------
# reStructuredText reads its own docinfo rather than a second mechanism
# ---------------------------------------------------------------------------

def test_rst_reads_a_leading_field():
    options, body = split(":layout: wide\n\nSome prose.\n", RST)
    assert options.layout == "wide"
    assert ":layout:" not in body
    assert "Some prose." in body


def test_rst_reads_a_field_under_a_title():
    text = "Title\n=====\n\n:layout: wide\n\nProse.\n"
    options, body = split(text, RST)
    assert options.layout == "wide"
    assert ":layout:" not in body
    assert "Title" in body and "Prose." in body


def test_rst_reads_a_field_under_an_overlined_title():
    text = "=====\nTitle\n=====\n\n:layout: wide\n\nProse.\n"
    options, body = split(text, RST)
    assert options.layout == "wide"
    assert ":layout:" not in body


def test_rst_leaves_every_other_docinfo_field_where_it_was():
    options, body = split(":author: Bob\n:layout: wide\n:version: 3\n\nProse.\n", RST)
    assert options.layout == "wide"
    assert ":author: Bob" in body and ":version: 3" in body
    assert ":layout:" not in body


def test_rst_ignores_a_field_list_further_down():
    text = "Prose first.\n\n:layout: wide\n"
    options, body = split(text, RST)
    assert options.layout == "default"
    assert body == text


def test_the_markdown_block_is_not_read_as_rst():
    # Each format gets one mechanism, which means neither gets the other's.
    options, body = split("---\nlayout: wide\n---\nbody\n", RST)
    assert options.layout == "default"


def test_the_rst_field_is_not_read_as_markdown():
    text = ":layout: wide\n\nbody\n"
    options, body = split(text)
    assert options.layout == "default"
    assert body == text


# ---------------------------------------------------------------------------
# End to end, through render()
# ---------------------------------------------------------------------------

def test_render_strips_the_block_instead_of_drawing_it():
    page = render.render("---\nlayout: wide\n---\n# Hi\n")
    assert page.options.layout == "wide"
    assert "<hr" not in page.html
    assert "layout" not in page.html
    assert "<h1" in page.html


def test_render_without_frontmatter_says_default():
    assert render.render("# Hi\n").options.layout == "default"


def test_the_pipeline_version_moved_with_the_behaviour():
    # A cached body from before this change was rendered from text that still
    # had the block in it, so the key must not name it.
    assert "+fswiki5" in render.renderer_id()


# ---------------------------------------------------------------------------
# The hard no
# ---------------------------------------------------------------------------

HOSTILE = [
    "banner: false", "acting: none", "_banner: ''", "impersonation: off",
    "style: 'display:none'", "class: acting", "layout: '\"><script>x</script>'",
    "layout: wide\nstyle: body{display:none}", "shell: none", "head: <script>",
]


@pytest.mark.parametrize("field", HOSTILE)
def test_frontmatter_cannot_reach_the_shell(field):
    """The one hard no in issue #5.

    The whole failure mode of impersonation is forgetting you are doing it, so
    the banner goes on every page rather than on a screen somebody has to think
    to visit. A document that could suppress it could make another person's
    wiki look like your own -- and that document is written by whoever can
    write a page, which is not the same person as the one reading it.
    """
    options, body = frontmatter.split(f"---\n{field}\n---\nbody\n")
    pages = Pages(client=None, banner="bob")
    html = pages.shell("Title", "<p>body</p>", "root.page", None, options)

    assert "viewing as bob" in html
    assert "class=acting" in html
    # Nothing the document wrote reached the page at all.
    for word in ("banner", "impersonation", "shell", "display:none", "<script>"):
        assert word not in html


def test_the_banner_is_there_with_the_layout_the_document_did_get():
    pages = Pages(client=None, banner="bob")
    html = pages.shell("Title", "<p>b</p>", "root.page", None,
                       frontmatter.Options(layout="wide"))
    assert "viewing as bob" in html
    assert "class=wide" in html


def test_a_layout_only_ever_names_a_class_this_file_knows():
    pages = Pages(client=None)
    for layout in frontmatter.LAYOUTS:
        html = pages.shell("T", "<p>b</p>", "root.p", None,
                           frontmatter.Options(layout=layout))
        assert "<body" in html
    # And the shell renders without any options at all, which is what every
    # error page in `pages` passes.
    assert "<body" in pages.shell("T", "<p>b</p>", "", None)
