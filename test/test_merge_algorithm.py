"""The three-way merge itself, as a function.

test_merge.py drives this through a mount, a draft, a push and a conflict,
which is the right way to check that the pieces are wired together — and the
wrong way to check what the merge does with awkward text, because reaching an
awkward case costs twenty seconds and a round trip. The cases here are the ones
worth being sure about and cheap to state.

Pure functions: no stack, no network.
"""

from __future__ import annotations

import pytest

from fswiki_core import merge as m

BASE = "alpha\nbravo\ncharlie\ndelta\necho\n"


def test_edits_that_do_not_touch_both_survive():
    mine = BASE.replace("alpha", "ALPHA")
    theirs = BASE.replace("echo", "ECHO")
    result = m.merge(BASE, mine, theirs)
    assert result.clean
    assert result.conflicts == 0
    assert result.text == "ALPHA\nbravo\ncharlie\ndelta\nECHO\n"


def test_both_sides_rewriting_one_line_is_a_conflict():
    result = m.merge(BASE, BASE.replace("charlie", "mine"),
                     BASE.replace("charlie", "theirs"))
    assert not result.clean
    assert result.conflicts == 1
    assert "mine" in result.text and "theirs" in result.text


def test_two_separate_clashes_are_counted_separately():
    """The number is what the user is told to expect to fix, so an
    off-by-one here is a promise the text does not keep."""
    mine = BASE.replace("alpha", "A1").replace("echo", "E1")
    theirs = BASE.replace("alpha", "A2").replace("echo", "E2")
    assert m.merge(BASE, mine, theirs).conflicts == 2


def test_the_labels_name_the_two_sides():
    result = m.merge(BASE, BASE.replace("charlie", "mine"),
                     BASE.replace("charlie", "theirs"),
                     mine_label="yours", theirs_label="revision 7")
    assert "yours" in result.text
    assert "revision 7" in result.text


def test_an_edit_the_server_already_has_is_redundant_not_clean_news():
    """Distinct from a clean merge: there is nothing to resolve *and* nothing
    left to publish. Telling someone their work merged cleanly when the answer
    is that it was already there sends them to push for no reason."""
    theirs = BASE.replace("charlie", "CHARLIE")
    result = m.merge(BASE, theirs, theirs)
    assert result.clean
    assert result.redundant
    assert result.text == theirs


def test_a_real_change_is_not_redundant():
    result = m.merge(BASE, BASE.replace("alpha", "ALPHA"), BASE)
    assert result.clean
    assert not result.redundant


def test_nothing_at_all_merges_to_nothing():
    """Every argument is nullable — a create has no base and a delete has no
    content — and none of those may raise."""
    assert m.merge(None, None, None).text == ""
    assert m.merge(None, None, None).redundant
    assert m.merge(None, "new\n", None).text == "new\n"


def test_markers_are_seven_characters_by_default():
    assert m.marker_width("plain text\n") == 7
    assert m.marker_width(None) == 7
    assert m.marker_width("", "", "") == 7


@pytest.mark.parametrize("token", ["<", "=", ">"])
def test_markers_grow_past_anything_already_in_the_text(token):
    """A wiki documenting a merge tool contains conflict markers, and markers
    we write must never be confusable with markers the author typed. jj solves
    this by growing; so do we."""
    text = f"how it looks:\n{token * 7}\ndone\n"
    assert m.marker_width(text) == 8
    assert m.marker_width(f"{token * 12}\n") == 13


def test_growth_looks_at_every_side_not_just_the_draft():
    """The ancestor can contain the marker even when neither edit does."""
    assert m.marker_width("<<<<<<<<<<\n", "clean\n", "clean\n") == 11
    assert m.marker_width("clean\n", "clean\n", ">>>>>>>>>>\n") == 11


def test_a_conflict_in_a_page_about_conflicts_is_still_readable():
    """End to end for the growth rule: the emitted markers must be longer than
    the ones in the content, or resolving the conflict by deleting marker lines
    would delete the page's own example."""
    base = "a marker looks like:\n=======\nthat is all\n"
    result = m.merge(base, base.replace("that is all", "mine"),
                     base.replace("that is all", "theirs"))
    assert result.conflicts == 1
    assert "========" in result.text          # ours, eight wide
    assert "\n=======\n" in result.text       # theirs, still seven, untouched


@pytest.mark.parametrize("token", ["<", "=", ">"])
def test_a_line_of_markers_is_unresolved(token):
    assert m.has_markers(f"before\n{token * 7}\nafter\n")
    assert m.has_markers(f"{token * 20} yours\n")


def test_a_heading_underline_is_not_a_conflict():
    """Setext headings underline with equals signs, and rejecting a push
    because someone wrote one would be maddening. Seven is the line: the
    marker is seven, and nobody underlines a five-letter heading with more."""
    assert not m.has_markers("Title\n=====\n")
    assert not m.has_markers("Title\n======\n")
    assert m.has_markers("Title\n=======\n")   # the documented cost of the rule


def test_markers_only_count_at_the_start_of_a_line():
    """Otherwise prose about the syntax could not be published at all."""
    assert not m.has_markers("the marker is <<<<<<< in git\n")
    assert not m.has_markers("  <<<<<<< indented, so not one\n")


def test_nothing_has_no_markers():
    assert not m.has_markers(None)
    assert not m.has_markers("")


def test_the_output_of_a_conflict_is_always_unresolved():
    """The two functions are a pair: whatever `merge` marks, `has_markers`
    must find, or push would publish half a merge. Including when the markers
    had to grow."""
    for base in (BASE, "=======\n" + BASE, "<<<<<<<<<<<<\n" + BASE):
        result = m.merge(base, base.replace("charlie", "mine"),
                         base.replace("charlie", "theirs"))
        assert result.conflicts == 1
        assert m.has_markers(result.text)


def test_a_clean_merge_never_looks_unresolved():
    result = m.merge(BASE, BASE.replace("alpha", "ALPHA"),
                     BASE.replace("echo", "ECHO"))
    assert not m.has_markers(result.text)
