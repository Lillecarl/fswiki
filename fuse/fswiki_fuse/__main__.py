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

import pyfuse3
import trio

from fswiki_core.client import Client, PostgrestError
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
    ap.add_argument("--read-only", action="store_true", help="refuse all writes")
    ap.add_argument(
        "--audit", action="store_true",
        help="identify the process behind every open from /proc. Costs "
             "microseconds against the round trip open() already makes, but it "
             "records what you do on your own machine, so it is opt-in",
    )
    ap.add_argument("--allow-other", action="store_true",
                    help="let other users see the mount; needs user_allow_other in fuse.conf")
    ap.add_argument("--debug", action="store_true", help="log every operation")
    ap.add_argument("--debug-fuse", action="store_true", help="also log libfuse's own chatter")
    return ap.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    client = Client(args.url, args.token)
    try:
        try:
            principal = await client.whoami()
        except PostgrestError as exc:
            log.error("cannot reach %s: %s", args.url, exc)
            return 1
        except OSError as exc:
            log.error("cannot reach %s: %s", args.url, exc)
            return 1

        if principal is None:
            if args.token:
                log.error("the token verified but resolves to no principal — "
                          "check oidc_issuer and oidc_subject in wiki.user_account")
                return 1
            log.warning("no token: mounting read-only, and anonymous sees nothing")

        fs = FswikiFs(
            client,
            principal,
            ttl=args.ttl,
            poll=args.poll,
            read_only=args.read_only or principal is None,
            audit=args.audit,
        )
        try:
            tree = await fs.refresh(force=True)
        except PostgrestError as exc:
            log.error("cannot read the wiki: %s", exc)
            return 1
        log.info("mounted %d entries at %s", len(tree.nodes), args.mountpoint)

        options = set(pyfuse3.default_options)
        options.add("fsname=fswiki")
        options.discard("default_permissions")
        if args.allow_other:
            options.add("allow_other")
        if args.debug_fuse:
            options.add("debug")

        pyfuse3.init(fs, args.mountpoint, options)
        try:
            await pyfuse3.main()
        finally:
            pyfuse3.close(unmount=True)
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

    try:
        return trio.run(run, args)
    except KeyboardInterrupt:
        log.info("unmounted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
