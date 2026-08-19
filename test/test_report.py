"""Everything the CLI prints. Nothing here talks to the network.

test_cli.py drives these through a real push against a real server and asserts
on a substring, which proves the wiring and almost nothing about the wording.
The wording is the product: `fswiki revert` is a destructive command whose
only safety rail is a sentence telling you what you are about to lose, and
`fswiki push` reports a rollback that the user cannot see any other way.

So the cases here are the ones a live stack reaches slowly or not at all — a
push where the third of four rows failed, a revert of a draft in the middle of
a merge, a plural that reads as a bug when it is wrong.

Pure functions: no stack, no network.
"""

from __future__ import annotations

import re

import pytest

from fswiki_cli import report

BASE = "alpha\nbravo\ncharlie\n"


@pytest.fixture(autouse=True)
def plain():
    """Colour off, so assertions are about words rather than escape codes.
    One test below puts it back to check the other half."""
    report.colour(enabled=False)
    yield
    report.colour(enabled=False)


def draft(operation="update", path="root.eng.guide", **extra):
    return {"operation": operation, "path": path, **extra}


def row(status="published", path="root.eng.guide", **extra):
    return {"status": status, "path": path, **extra}


# --- colour ----------------------------------------------------------------

def test_colour_is_a_switch_and_off_means_off():
    """Output is piped and redirected constantly, and escape codes in a file
    someone greps are worse than no colour at all."""
    report.colour(enabled=True)
    assert "\033[" in report.render_status([draft()])
    report.colour(enabled=False)
    assert "\033[" not in report.render_status([draft()])


def test_switching_colour_off_does_not_change_the_words():
    report.colour(enabled=True)
    coloured = report.render_status([draft(base_version=2)])
    report.colour(enabled=False)
    plain_text = report.render_status([draft(base_version=2)])
    assert re.sub(r"\033\[[0-9;]*m", "", coloured) == plain_text


# --- status ----------------------------------------------------------------

def test_nothing_pending_says_so():
    assert report.render_status([]) == "Nothing pending."


def test_one_change_is_singular_and_two_are_not():
    assert "1 pending change:" in report.render_status([draft()])
    assert "2 pending changes:" in report.render_status(
        [draft(path="root.a"), draft(path="root.b")])


@pytest.mark.parametrize("operation,label", [
    ("create", "new"),
    ("update", "modified"),
    ("delete", "retired"),
    ("move", "moved"),
])
def test_every_operation_reads_as_english(operation, label):
    assert label in report.render_status([draft(operation)])


def test_an_operation_we_do_not_know_is_printed_rather_than_dropped():
    """A row the user cannot see is a row they cannot act on, and the server
    may grow an operation before this client does."""
    assert "teleport" in report.render_status([draft("teleport")])


def test_the_revision_a_draft_was_edited_from_is_shown():
    """It is the number a conflict will be about."""
    assert "(from revision 4)" in report.render_status([draft(base_version=4)])


def test_a_draft_with_no_base_says_nothing_about_revisions():
    assert "revision" not in report.render_status([draft("create")])


def test_status_is_sorted_by_path_not_by_arrival():
    out = report.render_status([draft(path="root.zebra"), draft(path="root.apple")])
    assert out.index("apple") < out.index("zebra")


def test_a_clean_status_points_at_push():
    assert "fswiki push" in report.render_status([draft()])


def test_a_conflicted_status_points_somewhere_else():
    """Telling someone to push a draft that push will refuse is worse than
    saying nothing."""
    out = report.render_status([draft(state="conflicted", merged_from=9)])
    assert "unresolved merge with revision 9" in out
    assert "fswiki merge --abort" in out
    assert "fswiki push" not in out


# --- diff ------------------------------------------------------------------

def test_diff_of_nothing():
    assert report.render_diff([]) == "Nothing pending."


def test_a_content_change_is_a_unified_diff():
    out = report.render_diff([(draft(content="alpha\nBRAVO\ncharlie\n"), BASE)])
    assert "a/eng/guide" in out and "b/eng/guide" in out
    assert "-bravo" in out and "+BRAVO" in out


def test_a_create_diffs_against_nothing():
    out = report.render_diff([(draft("create", path="root.new", content="hi\n"), None)])
    assert "+hi" in out


def test_a_retirement_has_no_diff_to_show():
    out = report.render_diff([(draft("delete"), BASE)])
    assert "(retired)" in out
    assert "-alpha" not in out


def test_a_move_has_no_diff_to_show():
    """The content did not change; the path did, and the path is in the
    heading."""
    out = report.render_diff([(draft("move", path="root.moved"), BASE)])
    assert "(moved here)" in out
    assert "moved" in out


def test_a_draft_identical_to_the_published_text_says_so():
    """Printing an empty diff would look like a bug in the diff."""
    assert "(no content change)" in report.render_diff([(draft(content=BASE), BASE)])


def test_diffs_are_separated_and_sorted():
    out = report.render_diff([
        (draft(path="root.zebra", content="z\n"), None),
        (draft(path="root.apple", content="a\n"), None),
    ])
    assert out.index("apple") < out.index("zebra")
    assert "\n\n" in out


# --- push ------------------------------------------------------------------

def test_pushing_nothing_is_a_success():
    assert report.render_push([]) == ("Nothing to push.", True)


def test_a_clean_push_names_the_revisions():
    text, ok = report.render_push([row(version=8)])
    assert ok
    assert "Published 1 change." in text
    assert "-> revision 8" in text


def test_two_changes_are_plural():
    text, _ = report.render_push([row(path="root.a", version=1),
                                 row(path="root.b", version=2)])
    assert "Published 2 changes." in text


def test_every_row_is_reported_not_just_the_first():
    """push is all or nothing. A report that stopped at the first failure
    would leave the user fixing one problem at a time against a server that
    rolls back the whole changeset each try."""
    text, ok = report.render_push([
        row(path="root.a", version=1),
        row("conflict", path="root.b", server_version=5),
        row("forbidden", path="root.c"),
    ])
    assert not ok
    assert "a" in text and "b" in text and "c" in text
    assert "2 of 3" in text


def test_a_failed_push_says_the_drafts_are_still_there():
    """The single most important sentence in the command: the user has just
    been told something went wrong and needs to know their work survived."""
    text, ok = report.render_push([row("conflict")])
    assert not ok
    assert "Nothing was published and your drafts are untouched." in text


@pytest.mark.parametrize("status,label", [
    ("conflict", "CONFLICT"),
    ("unmerged", "UNMERGED"),
    ("forbidden", "FORBIDDEN"),
    ("missing", "MISSING"),
    ("invalid", "INVALID"),
])
def test_every_refusal_the_server_can_return(status, label):
    text, ok = report.render_push([row(status)])
    assert label in text
    assert not ok


def test_a_status_we_do_not_know_is_still_a_failure():
    """Unknown is not success. A client that treated an unrecognised status as
    published would report a rollback as a publish."""
    text, ok = report.render_push([row("something-new")])
    assert not ok
    assert "something-new" in text


def test_a_conflict_names_the_revision_the_server_is_at_now():
    text, _ = report.render_push([row("conflict", server_version=12)])
    assert "the server is now at revision 12" in text


def test_the_server_detail_is_passed_through():
    text, _ = report.render_push([row("forbidden", detail="you may not write here")])
    assert "you may not write here" in text


def test_a_published_row_does_not_carry_a_detail_line():
    """`detail` is set on rows the server had something to say about; echoing
    it under a success reads as a warning."""
    text, _ = report.render_push([row(version=3, detail="internal note")])
    assert "internal note" not in text


def test_a_conflict_says_which_kind_of_conflict_it_is():
    """The answer changes what to do next — a clean merge means run `fswiki
    merge`, three marked hunks means open the file — so it belongs here rather
    than behind another command."""
    text, _ = report.render_push(
        [row("conflict", base_content=BASE, server_content=BASE.replace("alpha", "A"))],
        [draft(content=BASE.replace("charlie", "C"))])
    assert "merges cleanly" in text


def test_a_push_with_a_conflict_points_at_merge():
    text, _ = report.render_push([row("conflict")])
    assert "fswiki merge" in text


def test_a_row_with_no_path_does_not_crash_the_report():
    """A refusal the server could not attribute to a path still has to be
    printed, because it is why the whole push rolled back."""
    text, ok = report.render_push([{"status": "invalid", "detail": "malformed"}])
    assert not ok
    assert "malformed" in text


# --- what kind of conflict -------------------------------------------------

def test_no_server_text_means_no_opinion():
    """Not every refusal is a conflict, and guessing would print merge advice
    under a permission error."""
    assert report.merge_outcome({}) is None
    assert report.merge_outcome({"base_content": BASE}) is None


def test_no_draft_means_no_opinion():
    """push() returns the server's text and the ancestor's but not the draft,
    because the client already has it. A caller that did not pass it gets
    silence rather than a merge against an empty string."""
    assert report.merge_outcome({"base_content": BASE, "server_content": BASE}) is None


def test_two_independent_creations_have_no_ancestor_to_merge_over():
    """A distinct outcome, and the one three-way merge cannot help with."""
    out = report.merge_outcome({"server_content": "theirs\n"}, draft(content="mine\n"))
    assert "no common ancestor" in out


def test_an_edit_the_server_already_has_is_reported_as_such():
    """Distinct from 'merges cleanly': there is nothing to resolve *and*
    nothing left to publish, and sending the user to merge and push for a
    no-op wastes both their steps."""
    theirs = BASE.replace("alpha", "A")
    out = report.merge_outcome({"base_content": BASE, "server_content": theirs},
                               draft(content=theirs))
    assert "already in the server's copy" in out


def test_hunks_are_counted_and_pluralised():
    mine = BASE.replace("alpha", "1").replace("charlie", "1")
    theirs = BASE.replace("alpha", "2").replace("charlie", "2")
    two = report.merge_outcome({"base_content": BASE, "server_content": theirs},
                               draft(content=mine))
    one = report.merge_outcome(
        {"base_content": BASE, "server_content": BASE.replace("alpha", "2")},
        draft(content=BASE.replace("alpha", "1")))
    assert "2 conflicting hunks" in two
    assert "1 conflicting hunk to resolve" in one


# --- push refused for markers ----------------------------------------------

def test_a_marked_draft_is_refused_by_name():
    out = report.render_marked([draft(path="root.eng.guide")])
    assert "1 draft" in out and "UNMERGED" in out
    assert "eng/guide" in out
    assert "Nothing was published." in out


def test_marked_drafts_are_plural_and_sorted():
    out = report.render_marked([draft(path="root.zebra"), draft(path="root.apple")])
    assert "2 drafts" in out
    assert out.index("apple") < out.index("zebra")


# --- merge -----------------------------------------------------------------

def test_merging_when_nothing_is_behind():
    assert "Nothing to merge" in report.render_merge([], [], [], applied=False)


def test_a_dry_run_says_it_changed_nothing_and_how_to_mean_it():
    out = report.render_merge([("eng/guide", 0)], [], [], applied=False)
    assert "Dry run" in out
    assert "fswiki merge --apply" in out


def test_an_applied_clean_merge_points_at_push():
    out = report.render_merge([("eng/guide", 0)], [], [], applied=True)
    assert "Merged 1 draft." in out
    assert "merged" in out
    assert "Push when you are happy" in out


def test_an_applied_merge_with_conflicts_does_not():
    """Push refuses anything still marked, so telling the user to push would
    be telling them to hit the refusal."""
    out = report.render_merge([], [("eng/guide", 3)], [], applied=True)
    assert "CONFLICT" in out
    assert "3 hunks need a human" in out
    assert "push refuses anything still marked" in out
    assert "Push when you are happy" not in out


def test_one_hunk_is_singular():
    assert "1 hunk need" in report.render_merge([], [("eng/guide", 1)], [], applied=True)


def test_a_draft_whose_ancestor_is_gone_is_reported_separately():
    """Nothing can be merged automatically without a base, and saying
    'conflict' would suggest a resolution that is not available."""
    out = report.render_merge([], [], ["eng/guide"], applied=True)
    assert "NO BASE" in out
    assert "merge it by hand" in out


def test_the_merged_count_covers_both_kinds():
    out = report.render_merge([("a", 0)], [("b", 1)], [], applied=True)
    assert "Merged 2 drafts." in out


# --- revert ----------------------------------------------------------------

def test_reverting_nothing():
    assert report.render_revert([], applied=False) == "Nothing pending."


def test_a_dry_run_is_the_warning():
    """The only safety rail on the one command in the project that destroys
    work no copy exists of."""
    out = report.render_revert([(draft(content="x\n"), BASE)], applied=False)
    assert "would be withdrawn" in out
    assert "This discards unpublished work. Nothing keeps a copy of it." in out
    assert "fswiki revert --apply" in out


def test_after_the_fact_it_is_past_tense_and_not_a_warning():
    out = report.render_revert([(draft(content="x\n"), BASE)], applied=True)
    assert "Withdrew 1 change:" in out
    assert "discards" not in out
    assert "Published history was never involved." in out


def test_the_cost_is_counted_against_the_published_text():
    """A 300-line page with one corrected typo loses one line. Reporting the
    draft's size would say 300 and frighten someone out of a safe operation."""
    published = "\n".join(f"line {i}" for i in range(300)) + "\n"
    edited = published.replace("line 7", "line seven")
    out = report.render_revert([(draft(content=edited, base_version=4), published)],
                               applied=False)
    assert "2 changed lines against revision 4" in out
    assert "300" not in out


def test_one_changed_line_is_singular():
    out = report.render_revert(
        [(draft(content="alpha\nbravo\n", base_version=1), "alpha\nbravo\ncharlie\n")],
        applied=False)
    assert "1 changed line against revision 1" in out


def test_a_create_loses_everything_because_it_is_nowhere_else():
    out = report.render_revert([(draft("create", content="a\nb\nc\n"), None)],
                               applied=False)
    assert "3 lines, published nowhere else" in out


def test_a_draft_that_changed_nothing_costs_nothing():
    out = report.render_revert([(draft(content=BASE), BASE)], applied=False)
    assert "no change against the published text" in out


def test_withdrawing_a_retirement_costs_no_text():
    out = report.render_revert([(draft("delete"), BASE)], applied=False)
    assert "the retirement is cancelled; the page stays published" in out


def test_withdrawing_a_move_costs_no_text():
    out = report.render_revert([(draft("move", path="root.moved"), BASE)],
                               applied=False)
    assert "the move is cancelled; the page stays where it is" in out


def test_an_update_with_no_published_side_is_treated_as_a_total_loss():
    """The published copy has become invisible, so there is nothing to count
    against and the honest answer is the whole draft."""
    out = report.render_revert([(draft(content="a\nb\n"), None)], applied=False)
    assert "2 lines, published nowhere else" in out


def test_a_draft_in_the_middle_of_a_merge_offers_the_reversible_route():
    """`merge --abort` restores from a copy the server kept; revert keeps no
    copy of anything. Someone about to lose a merge should be told the other
    door exists."""
    out = report.render_revert(
        [(draft(content="x\n", pre_merge_content=BASE), BASE)], applied=False)
    assert "a merge is outstanding here" in out
    assert "fswiki merge --abort" in out


def test_the_abort_hint_is_not_offered_when_there_is_no_merge():
    out = report.render_revert([(draft(content="x\n"), BASE)], applied=False)
    assert "merge --abort" not in out


def test_reverts_are_sorted_and_pluralised():
    out = report.render_revert([(draft(path="root.zebra", content="z\n"), None),
                                (draft(path="root.apple", content="a\n"), None)],
                               applied=False)
    assert "2 changes would be withdrawn" in out
    assert out.index("apple") < out.index("zebra")
