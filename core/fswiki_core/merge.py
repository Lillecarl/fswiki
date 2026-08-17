"""Three-way merge, on the client.

`wiki.push()` hands back all three sides of a conflict in one call — the draft
is *mine*, `server_content` is *theirs*, and `base_content` is the ancestor.
Merging them is deliberately client work: the server stores full checkouts and
adjudicates revisions, and it has no business deciding what someone meant.

Conflicts are marked, not hidden. Markers are the interchange format an editor
and a human can both read, and leaving them in the text is what lets the client
refuse to publish something still half-resolved.
"""

from __future__ import annotations

from dataclasses import dataclass

import merge3

# Deliberately the git/jj shape, because every editor with a merge mode already
# highlights it and every user has seen it before.
MINE = "<<<<<<<"
SPLIT = "======="
THEIRS = ">>>>>>>"


@dataclass
class Merged:
    """The outcome of merging one document."""

    text: str
    conflicts: int
    #: True when the merge changed nothing, i.e. the edit was already contained
    #: in what the server holds. Worth reporting separately: there is nothing to
    #: resolve *and* nothing to publish.
    redundant: bool = False

    @property
    def clean(self) -> bool:
        return self.conflicts == 0


def _lines(text: str | None) -> list[str]:
    return (text or "").splitlines(keepends=True)


def marker_width(*texts: str | None) -> int:
    """How long the markers must be to be unambiguous.

    Content can legitimately contain conflict markers — a wiki documenting a
    merge tool certainly does — and a seven-character marker inside the text
    would then be indistinguishable from one we wrote. jj solves this by growing
    its markers past anything already present, and so do we: the longest run of
    marker characters at the start of any line, plus one, floored at seven.
    """
    longest = 6
    for text in texts:
        for line in (text or "").splitlines():
            for token in (MINE[0], SPLIT[0], THEIRS[0]):
                if line.startswith(token * 7):
                    run = len(line) - len(line.lstrip(token))
                    longest = max(longest, run)
    return longest + 1


def merge(
    base: str | None,
    mine: str | None,
    theirs: str | None,
    *,
    mine_label: str = "yours",
    theirs_label: str = "server",
) -> Merged:
    """Merge `mine` and `theirs` over their common ancestor `base`."""
    width = marker_width(base, mine, theirs)
    m3 = merge3.Merge3(_lines(base), _lines(mine), _lines(theirs))

    conflicts = sum(1 for region in m3.merge_regions() if region[0] == "conflict")
    text = "".join(
        m3.merge_lines(
            name_a=mine_label,
            name_b=theirs_label,
            start_marker=MINE[0] * width,
            mid_marker=SPLIT[0] * width,
            end_marker=THEIRS[0] * width,
        )
    )
    return Merged(text=text, conflicts=conflicts, redundant=text == (theirs or ""))


def has_markers(text: str | None) -> bool:
    """Whether text still carries unresolved conflict markers.

    The check a client runs before publishing. Any line *starting* with a run of
    seven or more marker characters counts — matching mid-line would trip over
    prose, and matching fewer than seven would trip over a row of equals signs
    used as a heading underline.
    """
    for line in (text or "").splitlines():
        for token in (MINE[0], SPLIT[0], THEIRS[0]):
            if line.startswith(token * 7):
                return True
    return False
