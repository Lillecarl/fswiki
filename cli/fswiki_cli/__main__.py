"""fswiki — publish the drafts you made through the mount.

    fswiki status                 what you have pending
    fswiki diff                   what would change
    fswiki push -m "message"      publish all of it
    fswiki push -m "..." a/b.md   publish a subset

Push is all or nothing, the way `svn commit` is. If anything conflicts, nothing
is written and your drafts are left where they were, with the server's copy
attached so you can merge and try again.
"""

from __future__ import annotations

import argparse
import os
import sys

import anyio

from fswiki_core import merge
from fswiki_core.client import Client, PostgrestError

from . import paths, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="fswiki", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("FSWIKI_URL", "http://127.0.0.1:3000"),
                    help="PostgREST base URL (default: %(default)s)")
    ap.add_argument("--token", default=os.environ.get("FSWIKI_TOKEN"),
                    help="JWT; defaults to $FSWIKI_TOKEN")
    ap.add_argument("--no-colour", action="store_true", help="plain output")

    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="check the token resolves to a principal")
    sub.add_parser("status", help="list your pending drafts")

    p_diff = sub.add_parser("diff", help="show what push would change")
    p_diff.add_argument("paths", nargs="*", help="limit to these paths")

    p_merge = sub.add_parser(
        "merge",
        help="merge the server's changes into your conflicting drafts")
    p_merge.add_argument("paths", nargs="*", help="limit to these paths")
    p_merge.add_argument("--apply", action="store_true",
                         help="rewrite the drafts; without it, only report")
    p_merge.add_argument("--abort", action="store_true",
                         help="restore the drafts as they were before merging")

    p_push = sub.add_parser("push", help="publish drafts")
    p_push.add_argument("paths", nargs="*",
                        help="paths to publish; default is everything pending")
    p_push.add_argument("-m", "--message", help="revision message")
    p_push.add_argument("-n", "--dry-run", action="store_true",
                        help="show what would be published and stop")

    return ap.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    client = Client(args.url, args.token)
    try:
        try:
            principal = await client.whoami()
        except (PostgrestError, OSError) as exc:
            print(f"fswiki: cannot reach {args.url}: {exc}", file=sys.stderr)
            return 1

        if principal is None:
            print("fswiki: not authenticated — set FSWIKI_TOKEN or pass --token",
                  file=sys.stderr)
            return 1

        if args.command == "whoami":
            print(principal)
            return 0

        drafts = await client.drafts()

        if args.command == "status":
            print(report.render_status(drafts))
            return 0

        selected = _select(drafts, getattr(args, "paths", []))
        if selected is None:
            return 1

        if args.command == "diff" or getattr(args, "dry_run", False):
            entries = [(d, await _published_text(client, d)) for d in selected]
            print(report.render_diff(entries))
            if args.command == "diff":
                return 0
            print("\n" + report.dim("Dry run — nothing was published."))
            return 0

        if args.command == "merge":
            if args.abort:
                return await _abort_merge(client, selected)
            return await _merge(client, principal, selected, apply=args.apply)

        if not selected:
            print("Nothing to push.")
            return 0

        # Publishing a half-resolved merge is worse than not merging at all: the
        # markers become the document, and the next reader inherits them. This
        # is client-side on purpose — the server has no idea what a marker is,
        # and content that legitimately documents them must stay publishable.
        marked = [d for d in selected if merge.has_markers(d.get("content"))]
        if marked:
            print(report.render_marked(marked), file=sys.stderr)
            return 1

        # Nothing marked, so any outstanding merge is finished. This is keyed on
        # merged_from rather than on the conflicted flag because a *clean* merge
        # needs finishing too: it rewrote the text but deliberately left
        # base_version alone, so without this it would conflict against the very
        # revision it just merged in.
        #
        # Rebasing is separate from merging for one reason. A draft that claimed
        # to descend from the server's revision the moment it was merged would
        # become publishable as soon as someone deleted the markers, whether or
        # not they chose a side.
        for draft in selected:
            if draft.get("merged_from") is not None:
                if await client.resolve_merge(draft["path"]) is None:
                    print(f"fswiki: could not resolve the merge for "
                          f"{paths.to_display(draft['path'])}", file=sys.stderr)
                    return 1

        # None means "everything", which is not the same as an empty list.
        wanted = None if not args.paths else [d["path"] for d in selected]
        try:
            results = await client.push(args.message, wanted)
        except PostgrestError as exc:
            print(f"fswiki: push failed: {exc}", file=sys.stderr)
            return 1

        text, ok = report.render_push(results, selected)
        print(text)
        return 0 if ok else 1
    finally:
        await client.aclose()


async def _merge(client: Client, principal: str, drafts: list[dict],
                 *, apply: bool) -> int:
    """Merge the server's revisions into conflicting drafts.

    Reads only, until `--apply`. Merging rewrites work the user has not
    published, so the destructive reading of the word is the one that has to be
    asked for.

    Nothing here calls `push()`. Push is all-or-nothing and commits the moment
    every row is publishable, so using it to *ask* what conflicts would publish
    the drafts that do not. The manifest already carries each document's live
    revision, which is the same comparison `push()` makes, and both other sides
    of the merge are plain reads.
    """
    if not drafts:
        print("Nothing pending.")
        return 0

    manifest = await client.manifest()
    live = {row["id"]: row.get("version") for row in manifest}

    merged_ok, unresolved, skipped = [], [], []
    for draft in sorted(drafts, key=lambda d: d["path"]):
        display = paths.to_display(draft["path"])
        document_id, base_version = draft.get("document_id"), draft.get("base_version")
        current = live.get(document_id)

        if document_id is None or base_version is None:
            # A create never descended from anything, so there is no ancestor to
            # merge against. If it collided, the answer is a different name.
            continue
        if current is None or current == base_version:
            continue  # not conflicted; push will take it as it stands

        base = await client.revision(document_id, base_version)
        theirs = (await client.content(document_id)).decode("utf-8", errors="replace")
        if base is None:
            # The ancestor is gone — purged history, or a draft older than the
            # revisions still kept. Merging against nothing would silently take
            # one side, so refuse and say why.
            skipped.append(display)
            continue

        merged = merge.merge(base, draft.get("content") or "", theirs)

        if apply:
            try:
                # begin_merge, not put_draft: the server keeps the text this
                # replaced, so --abort has something to restore. Rebasing waits
                # until the merge is resolved — a conflicted draft that claimed
                # to descend from the server's revision would be publishable the
                # moment someone deleted the markers without choosing a side.
                updated = await client.begin_merge(
                    draft["path"], merged.text, current,
                    conflicted=not merged.clean,
                )
            except PostgrestError as exc:
                print(f"fswiki: could not rewrite the draft for {display}: {exc}",
                      file=sys.stderr)
                return 1
            if updated is None:
                print(f"fswiki: no draft of yours at {display}", file=sys.stderr)
                return 1

        (merged_ok if merged.clean else unresolved).append((display, merged.conflicts))

    print(report.render_merge(merged_ok, unresolved, skipped, applied=apply))
    return 1 if (unresolved or skipped) else 0


async def _abort_merge(client: Client, drafts: list[dict]) -> int:
    """Put the drafts back exactly as they were before a merge touched them."""
    restorable = [d for d in drafts if d.get("pre_merge_content") is not None]
    if not restorable:
        print("Nothing to back out — no draft has been merged.")
        return 0

    for draft in restorable:
        display = paths.to_display(draft["path"])
        try:
            if await client.abort_merge(draft["path"]) is None:
                print(f"fswiki: no draft of yours at {display}", file=sys.stderr)
                return 1
        except PostgrestError as exc:
            print(f"fswiki: could not back out {display}: {exc}", file=sys.stderr)
            return 1
        print(f"  {report.green('restored'.rjust(10))}  {display}")

    print("\n" + report.dim("Your drafts are as they were before the merge. "
                            "Published history was never involved."))
    return 0


async def _published_text(client: Client, draft: dict) -> str | None:
    """What the server currently holds for a draft, for the left side of a diff.

    None for a draft that creates something: there is nothing to compare against
    yet, and an empty string would render as "you deleted every line".
    """
    document_id = draft.get("document_id")
    if not document_id or draft["operation"] == "create":
        return None
    try:
        return (await client.content(document_id)).decode("utf-8", errors="replace")
    except (LookupError, PostgrestError):
        return None


def _select(drafts: list[dict], wanted: list[str]) -> list[dict] | None:
    """Filter drafts by the paths the user named, or return all of them."""
    if not wanted:
        return drafts

    by_path = {d["path"]: d for d in drafts}
    chosen: list[dict] = []
    for raw in wanted:
        try:
            path = paths.resolve(raw)
        except paths.PathError as exc:
            print(f"fswiki: {exc}", file=sys.stderr)
            return None
        draft = by_path.get(path)
        if draft is None:
            print(f"fswiki: no pending change for {raw} ({path})", file=sys.stderr)
            return None
        chosen.append(draft)
    return chosen


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report.colour(enabled=not args.no_colour and sys.stdout.isatty())
    try:
        return anyio.run(run, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
