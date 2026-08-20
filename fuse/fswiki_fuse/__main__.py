"""Mount the wiki.

    eval "$(fswiki-dev env)"
    fswiki-mount --token "$(fswiki-dev token bob)" ~/wiki

Runs in the foreground on trio, which is pyfuse3's native backend: no shim, and
cancellation behaves the way the FUSE protocol wants it to. httpx composes with
trio because httpcore is built on anyio.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import anyio
import pyfuse3
import trio

from fswiki_core.client import Client, PostgrestError, Unreachable
from .audit import AuditLog
from .fs import FswikiFs

log = logging.getLogger("fswiki")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="fswiki-mount", description=__doc__)
    ap.add_argument("mountpoint", help="an existing empty directory")
    ap.add_argument(
        "--url",
        default=os.environ.get("FSWIKI_URL", "http://127.0.0.1:3000"),
        help="PostgREST base URL (default: %(default)s)",
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("FSWIKI_TOKEN"),
        help="JWT; defaults to $FSWIKI_TOKEN",
    )
    ap.add_argument(
        "--ttl",
        type=float,
        default=5.0,
        help="how long the kernel is told it may cache entries and attributes "
             "(default: %(default)s)",
    )
    ap.add_argument(
        "--poll",
        type=float,
        default=2.0,
        help="seconds between change_token() checks; the manifest is re-fetched "
             "only when the token moves (default: %(default)s)",
    )
    ap.add_argument(
        "--backend",
        choices=("nfs", "fskit"),
        default=os.environ.get("FSWIKI_FUSE_BACKEND", "nfs"),
        help="FUSE-T transport on macOS (default: %(default)s; may also be "
             "set with $FSWIKI_FUSE_BACKEND)",
    )
    ap.add_argument("--read-only", action="store_true", help="refuse all writes")
    ap.add_argument(
        "--as", dest="act_as", metavar="USER",
        help="mount someone else's view of the wiki. Needs a grant on the "
             "server, which records that you did it. Always read-only",
    )
    ap.add_argument(
        "--as-group", dest="act_as_groups", metavar="GROUP", action="append",
        help="mount the view of somebody whose only memberships are these "
             "groups; repeatable, and meant to be. Name every group a real "
             "member would be in — one group alone is not anybody's view, and "
             "reads both too little and too much",
    )
    ap.add_argument(
        "--audit", action="store_true",
        help="identify the process behind every open from /proc and report it "
             "to the server. Costs microseconds against the round trip open() "
             "already makes, but it records what you do on your own machine, "
             "so it is opt-in",
    )
    ap.add_argument(
        "--audit-dir",
        default=os.environ.get("FSWIKI_AUDIT_DIR",
                               os.path.expanduser("~/.local/state/fswiki")),
        help="where the audit queue is spooled while offline (default: %(default)s)",
    )
    ap.add_argument(
        "--audit-interval", type=float, default=30.0,
        help="seconds between attempts to ship the queue (default: %(default)s)",
    )
    ap.add_argument(
        "--audit-argv", action="store_true",
        help="ship each caller's whole command line, not just the program "
             "name. Command lines routinely contain passwords and API keys "
             "that have nothing to do with the wiki, and this sends them to "
             "the server and into its backups. Only worth it where the fleet "
             "policy already says so",
    )
    ap.add_argument("--allow-other", action="store_true",
                    help="let other users see the mount; needs user_allow_other in fuse.conf")
    ap.add_argument("--debug", action="store_true", help="log every operation")
    ap.add_argument("--debug-fuse", action="store_true", help="also log libfuse's own chatter")
    return ap.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if sys.platform != "darwin" and args.backend != "nfs":
        log.error("--backend is a macOS FUSE-T option")
        return 1
    if args.act_as and args.act_as_groups:
        log.error("--as and --as-group are different questions; pick one")
        return 1
    if args.audit and not args.token:
        # Before the network, because this one is decidable from the arguments
        # alone: events are filed against a principal and anonymous is not one.
        # Left to the `principal is None` check further down it would arrive as
        # whatever the server says about an unauthenticated call, which sends
        # the reader to look at the connection instead of at the flag.
        log.error("--audit needs a token: events are filed against a "
                  "principal, and anonymous is not one")
        return 1
    acting_as = (args.act_as if args.act_as else
                 "a member of " + ", ".join(args.act_as_groups)
                 if args.act_as_groups else None)

    client = Client(args.url, args.token,
                    act_as=args.act_as, act_as_groups=args.act_as_groups)
    try:
        try:
            principal = await client.whoami()
        except PostgrestError as exc:
            # A refused impersonation arrives here as a 403, and calling that
            # "cannot reach" would send someone to look at the network when the
            # server answered perfectly clearly.
            if exc.status == 403 and acting_as:
                log.error("the server refused it: %s",
                          exc.body.get("message", exc)
                          if isinstance(exc.body, dict) else exc)
                return 1
            log.error("cannot reach %s: %s", args.url, exc)
            return 1
        except (Unreachable, OSError) as exc:
            log.error("cannot reach %s: %s", args.url, exc)
            return 1

        if principal is None:
            if args.token:
                log.error("the token verified but resolves to no principal — "
                          "check oidc_issuer and oidc_subject in wiki.user_account")
                return 1
            log.warning("no token: mounting read-only, and anonymous sees nothing")

        audit = None
        if args.audit and acting_as:
            # An impersonated request cannot write an access event -- the
            # transaction is read only by then, and the server has already
            # filed an impersonation_event instead, which is the truer record
            # of what happened. Enabling both would only build a queue that can
            # never ship.
            log.error("--audit and --as cannot be combined: an impersonated "
                      "read is recorded as an impersonation, not as an access")
            return 1

        if acting_as:
            # Said once, loudly, at the only moment anyone is looking at this
            # terminal — and after everything that could refuse, so it is never
            # printed about a mount that does not happen. The mount itself has
            # no banner to put it in, which is why every file in it is 0444 and
            # the mountpoint is `ro`.
            log.warning("mounting the view of %s — not yours. Read-only, and "
                        "the server has a record of it", acting_as)

        if args.audit:
            if principal is None:
                log.error("--audit needs a token: events are filed against a "
                          "principal, and anonymous is not one")
                return 1
            audit = AuditLog(client, args.audit_dir,
                             interval=args.audit_interval,
                             full_cmdline=args.audit_argv)
            if args.audit_argv:
                log.warning("--audit-argv: full command lines will be sent to "
                            "the server, including any secrets in them")

        fs = FswikiFs(
            client,
            principal,
            ttl=args.ttl,
            poll=args.poll,
            # Impersonation is read-only on the server, by a transaction the
            # client cannot influence. Setting it here too is not a second
            # implementation of that rule: it is so the refusal arrives at
            # open() as EROFS, where an editor can act on it, instead of at
            # save time as a 25006 the user cannot do anything about.
            read_only=args.read_only or principal is None or bool(acting_as),
            show_drafts=not args.read_only,
            audit=audit,
        )
        try:
            tree = await fs.refresh(force=True)
        except (PostgrestError, Unreachable) as exc:
            log.error("cannot read the wiki: %s", exc)
            return 1
        log.info("mounting %d entries at %s", len(tree.nodes), args.mountpoint)

        options = set(pyfuse3.default_options)
        options.add("fsname=fswiki")
        if sys.platform == "darwin":
            options.add(f"backend={args.backend}")
            if args.backend == "nfs":
                # NFSv4 represents extended attributes as named attributes.
                # FUSE-T can disable that bridge; request it explicitly so
                # fswiki's metadata reaches getxattr/listxattr.
                options.add("namedattr")
        options.discard("default_permissions")
        if fs.read_only:
            # The kernel then refuses writes before they ever reach us, which
            # is the same move as `set transaction read only` on the server and
            # as the preview server refusing methods before routing: a property
            # of the mount rather than an inventory of the handlers that
            # remembered to check. The handlers still check, because two
            # independent enforcements is what makes a property one.
            options.add("ro")
        if args.allow_other:
            options.add("allow_other")
        if args.debug_fuse:
            options.add("debug")

        pyfuse3.init(fs, args.mountpoint, options)
        try:
            async with anyio.create_task_group() as tg:
                if audit is not None:
                    # Ships in the background; recording never waits on it.
                    tg.start_soon(audit.run)
                await pyfuse3.main()
                tg.cancel_scope.cancel()
        finally:
            pyfuse3.close(unmount=True)
            if audit is not None:
                # One last attempt, so unmounting does not strand a queue that
                # would otherwise sit there until the next mount.
                with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                    try:
                        await audit.flush()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not flush the audit queue: %s", exc)
        return 0
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not args.debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    if not os.path.isdir(args.mountpoint):
        log.error("%s is not a directory", args.mountpoint)
        return 1
    if sys.platform == "darwin":
        helper = os.environ.get("FUSE_NFSSRV_PATH")
        if not helper or not os.access(helper, os.X_OK):
            log.error(
                "FUSE-T is not installed: expected an executable helper at %s",
                helper or "/Library/Application Support/fuse-t/bin/go-nfsv4",
            )
            return 1

    try:
        return trio.run(run, args)
    except KeyboardInterrupt:
        log.info("unmounted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
