"""fswiki — publish the drafts you made through the mount.

    fswiki status                 what you have pending
    fswiki diff                   what would change
    fswiki push -m "message"      publish all of it
    fswiki revert                 throw a draft away
    fswiki preview                read it in a browser while you write
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

from fswiki_core import merge, render
from fswiki_core.client import Client, PostgrestError, Unreachable

from . import paths, preview, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="fswiki", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("FSWIKI_URL", "http://127.0.0.1:3000"),
                    help="PostgREST base URL (default: %(default)s)")
    ap.add_argument("--token", default=os.environ.get("FSWIKI_TOKEN"),
                    help="JWT; defaults to $FSWIKI_TOKEN")
    ap.add_argument("--no-colour", action="store_true", help="plain output")
    # Read-only by construction on the server, so there is no --apply to guard
    # here and no command that needs to refuse itself: an impersonated write is
    # refused by the transaction it runs in. See docs/impersonation.md.
    ap.add_argument("--as", dest="act_as", metavar="USER",
                    help="see the wiki as this person (needs a grant)")
    ap.add_argument("--as-group", dest="act_as_groups", metavar="GROUP",
                    action="append",
                    help="see it as a member of these groups; repeatable. "
                         "Name every group a real member would be in — one "
                         "group alone is not a person's view")

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

    p_render = sub.add_parser(
        "render", help="render a document to HTML")
    p_render.add_argument("path", nargs="?", help="the document to render")
    p_render.add_argument("--backend", help="which render backend to use")
    p_render.add_argument("--draft", action="store_true",
                          help="render your draft rather than what is published")
    p_render.add_argument("--list-backends", action="store_true",
                          help="print the registered backends and stop")
    p_render.add_argument("--raw", action="store_true",
                          help="leave wiki links unresolved, as they are cached")

    p_preview = sub.add_parser(
        "preview", help="serve the wiki as HTML, for looking at while you write")
    p_preview.add_argument("--host", default="127.0.0.1",
                           help="address to bind (default: %(default)s). Anything "
                                "else exposes your view of the wiki to whoever "
                                "reaches the port")
    p_preview.add_argument("--port", type=int, default=8222,
                           help="port to bind (default: %(default)s)")
    p_preview.add_argument("--backend", help="which render backend to use")
    p_preview.add_argument("--published", action="store_true",
                           help="ignore your drafts and show what is published")

    p_revert = sub.add_parser(
        "revert",
        help="withdraw drafts, putting the files back to what is published")
    p_revert.add_argument("paths", nargs="*", help="limit to these paths")
    p_revert.add_argument("--apply", action="store_true",
                          help="actually withdraw them; without it, only report")

    p_push = sub.add_parser("push", help="publish drafts")
    p_push.add_argument("paths", nargs="*",
                        help="paths to publish; default is everything pending")
    p_push.add_argument("-m", "--message", help="revision message")
    p_push.add_argument("-n", "--dry-run", action="store_true",
                        help="show what would be published and stop")

    return ap.parse_args(argv)


def _acting_as(args: argparse.Namespace) -> str | None:
    """How to describe the borrowed identity to a human, or None.

    Names rather than uuids, and the group form says "a member of" because that
    is what it is: not the group, but somebody whose only memberships are those.
    Getting this wording wrong is how the two get confused, which is the exact
    confusion the group-set model exists to prevent.
    """
    if args.act_as:
        return args.act_as
    if args.act_as_groups:
        return "a member of " + ", ".join(args.act_as_groups)
    return None


async def run(args: argparse.Namespace) -> int:
    if args.command == "render" and args.list_backends:
        for backend in render.available():
            types = ", ".join(backend.content_types)
            print(f"  {backend.name:<16} {backend.version:<10} {types}")
        return 0

    if args.act_as and args.act_as_groups:
        print("fswiki: --as and --as-group are different questions; pick one",
              file=sys.stderr)
        return 1
    client = Client(args.url, args.token,
                    act_as=args.act_as, act_as_groups=args.act_as_groups)
    try:
        try:
            principal = await client.whoami()
        except (PostgrestError, Unreachable, OSError) as exc:
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

        if args.command == "render":
            return await _render(client, drafts, args)

        if args.command == "preview":
            return await preview.serve(client, host=args.host, port=args.port,
                                       backend=args.backend,
                                       drafts=not args.published,
                                       acting_as=_acting_as(args))

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

        if args.command == "revert":
            return await _revert(client, selected, apply=args.apply)

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


async def _render(client: Client, drafts: list[dict],
                  args: argparse.Namespace) -> int:
    """One document to HTML on stdout.

    The composable half of previewing, and the previewer's own inner loop. It
    prints to stdout so it can be tested by a shell script like everything
    else here, and piped into a browser by anyone who has not waited for
    `fswiki preview`.
    """
    if not args.path:
        print("fswiki: render needs a path (or --list-backends)", file=sys.stderr)
        return 1
    try:
        path = paths.resolve(args.path)
    except paths.PathError as exc:
        print(f"fswiki: {exc}", file=sys.stderr)
        return 1

    draft = next((d for d in drafts if d["path"] == path), None)
    content_type = "text/markdown"

    if args.draft or (draft is not None and draft.get("content") is not None):
        if draft is None or draft.get("content") is None:
            print(f"fswiki: no draft of yours at {paths.to_display(path)}",
                  file=sys.stderr)
            return 1
        text = draft["content"]
        content_type = draft.get("content_type") or content_type
    else:
        row = await client.document(path)
        if row is None:
            print(f"fswiki: no document at {paths.to_display(path)}",
                  file=sys.stderr)
            return 1
        text = row.get("content") or ""
        content_type = row.get("content_type") or content_type

    try:
        page = render.render(text, content_type=content_type,
                             backend=args.backend)
    except render.UnknownBackend as exc:
        print(f"fswiki: {exc}", file=sys.stderr)
        return 1
    except render.safety.SanitiserUnavailable as exc:
        print(f"fswiki: {exc}", file=sys.stderr)
        return 1

    if args.raw:
        # What a shared cache would hold: links unresolved, because which of
        # them are live is a property of the reader and not of the revision.
        print(page.html, end="")
        return 0

    # Resolved against this caller. A document absent from the manifest is
    # indistinguishable here from one they may not read, which is the point.
    visible = {d["path"] for d in await client.manifest()}
    print(render.links.resolve(
        page.html,
        lambda target: f"/{paths.to_display(target)}" if target in visible else None,
    ), end="")
    return 0


async def _revert(client: Client, drafts: list[dict], *, apply: bool) -> int:
    """Withdraw drafts. The file goes back to whatever is published.

    A dry run unless --apply, on the same reasoning as merge: this rewrites
    work that has not been published. It is in fact the stronger case of the
    two. `merge --abort` restores from `pre_merge_content`, a copy the server
    kept on purpose; revert deletes the draft row and with it the only copy of
    the text that ever existed. There is no undo, so the default is to say what
    would happen.

    The published side is fetched even for the dry run, because the useful
    number is how much would change, not how big the draft is.
    """
    if not drafts:
        print("Nothing pending.")
        return 0

    entries = [(d, await _published_text(client, d)) for d in drafts]

    if not apply:
        print(report.render_revert(entries, applied=False))
        return 0

    for draft, _ in entries:
        display = paths.to_display(draft["path"])
        try:
            # False, not an exception, when the draft is someone else's: RLS
            # filters the delete rather than refusing it. Silence would read as
            # success and leave the file looking reverted when it is not.
            if not await client.delete_draft(draft["path"]):
                print(f"fswiki: no draft of yours at {display}", file=sys.stderr)
                return 1
        except PostgrestError as exc:
            print(f"fswiki: could not withdraw {display}: {exc}", file=sys.stderr)
            return 1

    print(report.render_revert(entries, applied=True))
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
