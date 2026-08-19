"""Wikilinks: the rewrite on the way in, and the resolution on the way out.

test_render.py checks that every backend agrees about these, which is what a
plugin seam needs. This is the other half: the edge cases in the rewriting
itself, which are the same whichever engine runs afterwards, and where getting
it wrong is a leak rather than a rendering difference.

The rule the module exists for: **a link the reader may not follow renders as
plain text, and as exactly the same plain text as a link to something that is
not there.** A live link to a document you may not read discloses that it
exists, where it lives and what it is called — three things the ACL did not
grant, disclosed in the HTML before any click could reach the audit trail.

Pure functions: no stack, no network.
"""

from __future__ import annotations

import pytest

from fswiki_core.render import links

P = links.PREFIX


def deny(_path):
    return None


def allow(path):
    return f"/w/{path}"


# --- expanding -------------------------------------------------------------

def test_a_display_path_becomes_an_ltree_path():
    assert links.expand("[[public/welcome]]") == f"[public/welcome]({P}root.public.welcome)"


def test_an_ltree_path_is_taken_as_one():
    """People copy paths out of error messages and reports, and those print
    ltree."""
    assert links.expand("[[root.public.welcome]]") == \
        f"[root.public.welcome]({P}root.public.welcome)"
    assert links.expand("[[root]]") == f"[root]({P}root)"


def test_a_label_replaces_the_shown_text_but_not_the_target():
    assert links.expand("[[public/welcome|Start here]]") == \
        f"[Start here]({P}root.public.welcome)"


def test_an_empty_label_falls_back_to_the_target():
    """`[[a/b|]]` is a typo, and rendering an empty anchor makes an
    unclickable link nobody can see to fix."""
    assert links.expand("[[public/welcome|]]") == \
        f"[public/welcome]({P}root.public.welcome)"
    assert links.expand("[[public/welcome|   ]]") == \
        f"[public/welcome]({P}root.public.welcome)"


def test_surrounding_space_is_not_part_of_the_name():
    assert links.expand("[[  public/welcome  ]]") == \
        f"[public/welcome]({P}root.public.welcome)"


def test_an_empty_target_is_left_alone():
    assert links.expand("[[]]") == "[[]]"
    assert links.expand("[[   ]]") == "[[   ]]"


def test_a_target_the_wiki_could_never_hold_stays_literal_text():
    """Inventing a link to a path that cannot exist would produce a permanent
    404 that looks like a missing page rather than a typo."""
    assert links.expand("[[my docs/guide]]") == "[[my docs/guide]]"
    assert links.expand("[[a/report.tar.gz]]") == "[[a/report.tar.gz]]"


def test_brackets_inside_a_link_stop_it_being_one():
    """Neither the target nor the label may contain a bracket — the pattern
    excludes them on both sides — so a link that tries stays literal text
    rather than matching up to the wrong `]]`. That also means the label
    escaping is belt and braces: nothing that reaches it can contain a bracket
    to escape."""
    assert links.expand("[[public/welcome|see [1] here]]") == \
        "[[public/welcome|see [1] here]]"
    assert links.expand("[[a[b]/c]]") == "[[a[b]/c]]"


def test_an_unclosed_bracket_does_not_swallow_the_page():
    """Deliberately not matching across newlines. A single stray `[[` in a
    long document should cost one line, not the rest of the file."""
    text = "[[unclosed\nand the rest of the page\n"
    assert links.expand(text) == text


def test_several_links_on_one_line():
    out = links.expand("see [[a/b]] and [[c/d|D]]")
    assert out == f"see [a/b]({P}root.a.b) and [D]({P}root.c.d)"


def test_an_ordinary_markdown_link_is_untouched():
    assert links.expand("see [a link](http://x/) and [not a wikilink]") == \
        "see [a link](http://x/) and [not a wikilink]"


def test_a_wikilink_inside_a_code_span_is_expanded_anyway():
    """The documented cost of recognising these before the backend: `expand`
    is a regex over source text and has no idea what a code span is. A page
    documenting the wikilink syntax cannot show it as typed — which is a wart
    worth knowing about, and cheaper than teaching every backend the syntax.

    Not a leak: the target is still resolved per reader like any other, so the
    worst case is a code sample that reads oddly."""
    assert links.expand("write `[[a/b]]`") == f"write `[a/b]({P}root.a.b)`"


def test_a_caller_may_supply_its_own_interpretation():
    """The seam exists so a caller with a different notion of where a name
    points — a preview server rooted somewhere else — does not have to
    reimplement the matching."""
    assert links.expand("[[anything]]", to_path=lambda t: f"root.{t}.fixed") == \
        f"[anything]({P}root.anything.fixed)"


def test_a_supplied_interpretation_that_refuses_leaves_the_text_alone():
    def refuse(_target):
        raise ValueError("not here")
    assert links.expand("[[whatever]]", to_path=refuse) == "[[whatever]]"


# --- resolving -------------------------------------------------------------

def test_a_permitted_link_becomes_a_real_one():
    out = links.resolve(f'<p><a href="{P}root.a.b">B</a></p>', allow)
    assert out == '<p><a href="/w/root.a.b">B</a></p>'
    assert links.unresolved(out) == 0


def test_a_refused_link_keeps_its_text_and_loses_its_anchor():
    out = links.resolve(f'<p><a href="{P}root.a.b">B</a></p>', deny)
    assert out == "<p>B</p>"


def test_forbidden_and_missing_are_byte_for_byte_identical():
    """The property the whole module is built around. `allow` returns None for
    both, and nothing downstream may be able to tell which it was."""
    forbidden = links.resolve(f'<p><a href="{P}root.secret.plans">X</a></p>', deny)
    missing = links.resolve(f'<p><a href="{P}root.gone.away">X</a></p>', deny)
    assert forbidden == missing == "<p>X</p>"


def test_markup_inside_a_refused_link_survives():
    """Only the anchor is dropped. Losing the emphasis as well would make the
    sentence read differently for one reader than another."""
    out = links.resolve(f'<p><a href="{P}root.a.b">see <em>this</em></a></p>', deny)
    assert out == "<p>see <em>this</em></p>"


def test_an_ordinary_link_is_not_touched():
    """Only our own prefix is ours to rewrite."""
    body = '<p><a href="https://example.com/" rel="noopener">out</a></p>'
    assert links.resolve(body, deny) == body


def test_an_anchor_with_no_href_at_all():
    assert links.resolve('<p><a name="top">t</a></p>', deny) == '<p><a name="top">t</a></p>'


def test_a_valueless_attribute_stays_valueless():
    """HTML allows bare attributes, and rewriting `<input disabled>` into
    `<input disabled="">` would be a silent change to somebody's page."""
    assert links.resolve('<input disabled>', deny) == '<input disabled>'


def test_a_self_closing_tag_stays_self_closing():
    assert links.resolve('<img src="/x.png" />', deny) == '<img src="/x.png" />'


def test_attribute_values_are_escaped_on_the_way_back_out():
    """The parser hands back decoded values, so re-emitting them raw would
    turn a quoted title into an attribute injection."""
    out = links.resolve('<img src="/x.png" title="a &quot;quote&quot;">', deny)
    assert '&quot;' in out
    assert out.count('"') % 2 == 0


def test_entities_and_character_references_survive_the_round_trip():
    """convert_charrefs is off, so these arrive as their own events and have
    to be put back — otherwise `&amp;` in someone's page becomes `&`."""
    assert links.resolve("<p>a &amp; b &#8212; c</p>", deny) == "<p>a &amp; b &#8212; c</p>"


def test_text_is_escaped_but_not_over_escaped():
    """Quotes in prose are prose. Escaping them would litter every page that
    quotes anything with &quot;."""
    out = links.resolve('<p>a < b & "c"</p>', deny)
    assert "&lt;" in out and "&amp;" in out and '"c"' in out


def test_a_comment_is_stripped():
    assert links.resolve("<p>a<!-- note -->b</p>", deny) == "<p>ab</p>"


def test_counting_unresolved_anchors():
    """Serving a cached body without resolving it is the mistake this exists
    to catch: the links are inert, but the paths are in the DOM."""
    body = f'<a href="{P}root.a">a</a><a href="{P}root.b">b</a><a href="/w/x">x</a>'
    assert links.unresolved(body) == 2
    assert links.unresolved(links.resolve(body, allow)) == 0
    assert links.unresolved(links.resolve(body, deny)) == 0


def test_resolution_is_per_reader_over_the_same_body():
    """The reason resolution is a separate step: the body is a function of the
    revision and caches forever, and who may follow what is a function of the
    ACL and cannot."""
    body = f'<p><a href="{P}root.a.b">B</a></p>'
    assert links.resolve(body, allow) != links.resolve(body, deny)
    assert links.resolve(body, allow) == '<p><a href="/w/root.a.b">B</a></p>'
