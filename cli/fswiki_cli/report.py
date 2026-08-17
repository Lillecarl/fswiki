"""Rendering. Nothing here talks to the network."""

from __future__ import annotations

import difflib

from fswiki_core import merge as merge3way

from . import paths

_enabled = True


def colour(*, enabled: bool) -> None:
    global _enabled
    _enabled = enabled


def _wrap(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _enabled else text


def bold(t: str) -> str:
    return _wrap("1", t)


def dim(t: str) -> str:
    return _wrap("2", t)


def red(t: str) -> str:
    return _wrap("31", t)


def green(t: str) -> str:
    return _wrap("32", t)


def yellow(t: str) -> str:
    return _wrap("33", t)


# How each draft operation reads in a listing, and how push reports its outcome.
_OP_LABEL = {
    "create": ("new", green),
    "update": ("modified", yellow),
    "delete": ("retired", red),
    "move": ("moved", yellow),
}

_STATUS_LABEL = {
    "published": ("published", green),
    "conflict": ("CONFLICT", red),
    "unmerged": ("UNMERGED", red),
    "forbidden": ("FORBIDDEN", red),
    "missing": ("MISSING", red),
    "invalid": ("INVALID", red),
}


def merge_outcome(row: dict, draft: dict | None = None) -> str | None:
    """One line on whether a conflicting row can be merged, or None if unknown.

    `push()` returns the server's text and the ancestor's, but not the draft —
    the client already has that. Callers that have the draft to hand should pass
    it; without it there is nothing to merge and this says so.
    """
    base, theirs = row.get("base_content"), row.get("server_content")
    if theirs is None:
        return None
    if base is None:
        return yellow("no common ancestor — this path was created twice, "
                      "independently")
    if draft is None:
        return None

    merged = merge3way.merge(base, draft.get("content") or "", theirs)
    if merged.redundant:
        return dim("your edit is already in the server's copy")
    if merged.clean:
        return green("merges cleanly")
    return yellow(f"{merged.conflicts} conflicting "
                  f"{'hunk' if merged.conflicts == 1 else 'hunks'} to resolve")


def render_marked(drafts: list[dict]) -> str:
    """Refuse to publish drafts that still carry conflict markers."""
    lines = [bold(red(f"Push refused: {len(drafts)} draft"
                      f"{'s' if len(drafts) != 1 else ''} still contain "
                      f"unresolved conflict markers.")), ""]
    for draft in sorted(drafts, key=lambda d: d["path"]):
        lines.append(f"  {red('UNMERGED'.rjust(10))}  {paths.to_display(draft['path'])}")
    lines += [
        "",
        dim("Resolve them through the mount — delete the markers and the side"),
        dim("you do not want — then push again. Nothing was published."),
    ]
    return "\n".join(lines)


def render_merge(clean: list[tuple[str, int]],
                 conflicted: list[tuple[str, int]],
                 skipped: list[str],
                 *, applied: bool) -> str:
    """Report one merge run."""
    if not (clean or conflicted or skipped):
        return "Nothing to merge — no draft is behind the server."

    lines: list[str] = []
    for display, _ in clean:
        lines.append(f"  {green('merged'.rjust(10))}  {display}")
    for display, count in conflicted:
        lines.append(f"  {red('CONFLICT'.rjust(10))}  {display}")
        lines.append(dim(f"              {count} hunk{'s' if count != 1 else ''} "
                         f"need a human"))
    for display in skipped:
        lines.append(f"  {red('NO BASE'.rjust(10))}  {display}")
        lines.append(dim("              the revision it was edited from is gone; "
                         "merge it by hand"))

    if not applied:
        header = bold("Dry run — nothing was changed.")
        footer = ["", dim("Rewrite the drafts with: fswiki merge --apply")]
        return "\n".join([header, "", *lines, *footer])

    header = bold(f"Merged {len(clean) + len(conflicted)} draft"
                  f"{'s' if len(clean) + len(conflicted) != 1 else ''}.")
    footer = ["", bold("Your drafts now contain the merge result.")]
    if conflicted:
        footer += [
            dim("Conflicted hunks are marked in the text. Resolve them through"),
            dim("the mount, then push — push refuses anything still marked."),
        ]
    else:
        footer.append(dim("Push when you are happy with them."))
    return "\n".join([header, "", *lines, *footer])


def render_status(drafts: list[dict]) -> str:
    if not drafts:
        return "Nothing pending."

    lines = [bold(f"{len(drafts)} pending change{'s' if len(drafts) != 1 else ''}:"), ""]
    conflicted = 0
    for draft in sorted(drafts, key=lambda d: d["path"]):
        label, paint = _OP_LABEL.get(draft["operation"], (draft["operation"], dim))
        base = draft.get("base_version")
        suffix = dim(f"  (from revision {base})") if base is not None else ""
        lines.append(f"  {paint(label.rjust(9))}  {paths.to_display(draft['path'])}{suffix}")
        if draft.get("state") == "conflicted":
            conflicted += 1
            lines.append(red(f"             unresolved merge with revision "
                             f"{draft.get('merged_from')}"))
    lines.append("")
    if conflicted:
        lines.append(dim("Resolve the markers through the mount, then push."))
        lines.append(dim("Or back out entirely with: fswiki merge --abort"))
    else:
        lines.append(dim("Publish with: fswiki push -m \"...\""))
    return "\n".join(lines)


def render_diff(entries: list[tuple[dict, str | None]]) -> str:
    """Unified diffs for the drafts, published text on the left."""
    if not entries:
        return "Nothing pending."

    chunks: list[str] = []
    for draft, published in sorted(entries, key=lambda e: e[0]["path"]):
        display = paths.to_display(draft["path"])
        operation = draft["operation"]

        if operation == "delete":
            chunks.append(red(f"--- {display}  (retired)"))
            continue
        if operation == "move":
            chunks.append(yellow(f"~~~ {display}  (moved here)"))
            continue

        new_text = draft.get("content") or ""
        old_text = published or ""
        if old_text == new_text:
            chunks.append(dim(f"=== {display}  (no content change)"))
            continue

        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{display}",
            tofile=f"b/{display}",
            n=3,
        )
        rendered = []
        for line in diff:
            line = line.rstrip("\n")
            if line.startswith("+++") or line.startswith("---"):
                rendered.append(bold(line))
            elif line.startswith("@@"):
                rendered.append(dim(line))
            elif line.startswith("+"):
                rendered.append(green(line))
            elif line.startswith("-"):
                rendered.append(red(line))
            else:
                rendered.append(line)
        chunks.append("\n".join(rendered))

    return "\n\n".join(chunks)


def render_push(results: list[dict], drafts: list[dict] | None = None) -> tuple[str, bool]:
    """Report one push. Returns the text and whether everything published.

    Every row matters: push is all or nothing, so a single non-published status
    means the whole changeset was rolled back and the drafts are still there.
    Reporting only the first row would be actively misleading.
    """
    if not results:
        return ("Nothing to push.", True)

    ok = all(r.get("status") == "published" for r in results)
    by_path = {d["path"]: d for d in (drafts or [])}
    lines: list[str] = []

    for row in sorted(results, key=lambda r: r.get("path") or ""):
        status = row.get("status", "?")
        label, paint = _STATUS_LABEL.get(status, (status, dim))
        display = paths.to_display(row.get("path") or "")
        line = f"  {paint(label.rjust(10))}  {display}"
        if status == "published" and row.get("version") is not None:
            line += dim(f"  -> revision {row['version']}")
        lines.append(line)

        # `version` is the revision that *was published*, so it is null on any
        # non-published row — there is no base version to report here. `detail`
        # carries the "edited from X but the server is at Y" wording already.
        if status == "conflict" and row.get("server_version") is not None:
            lines.append(dim(f"              the server is now at revision "
                             f"{row['server_version']}"))
        detail = row.get("detail")
        if detail and status != "published":
            lines.append(dim(f"              {detail}"))

        # push() returns all three sides, so we can say whether this is a
        # conflict anyone has to think about or just two edits that happen not
        # to touch. The answer changes what the user should do next, so it
        # belongs here rather than behind another command.
        if status == "conflict":
            outcome = merge_outcome(row, by_path.get(row.get("path")))
            if outcome is not None:
                lines.append("              " + outcome)

    if any(r.get("status") == "conflict" for r in results):
        lines.append("")
        lines.append(dim("Merge them with: fswiki merge"))

    if ok:
        header = bold(green(f"Published {len(results)} change"
                            f"{'s' if len(results) != 1 else ''}."))
        return ("\n".join([header, "", *lines]), True)

    failed = [r for r in results if r.get("status") != "published"]
    header = bold(red(f"Push refused: {len(failed)} of {len(results)} "
                      f"could not be applied."))
    footer = [
        "",
        bold("Nothing was published and your drafts are untouched."),
        dim("Push is all or nothing. Resolve the entries above and try again;"),
        dim("for a conflict, re-read the file to get the server's version."),
    ]
    return ("\n".join([header, "", *lines, *footer]), False)
