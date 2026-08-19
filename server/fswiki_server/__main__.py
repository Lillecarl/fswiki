"""fswiki-serve: migrate, start PostgREST, serve.

Three phases, in that order, and the order is the program. PostgREST builds
its schema cache when it connects, so it must not connect until the schema has
stopped changing; and nothing should be served until PostgREST answers, or the
first visitor meets a connection refused that looks like a bug here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from .app import Application
from .config import Config, ConfigError
from .migrate import MigrationError, migrate
from .postgrest import Postgrest, PostgrestError

log = logging.getLogger("fswiki.server")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="fswiki-serve",
        description="Serve the wiki to browsers. Configuration is environment "
                    "first; these override it.")
    p.add_argument("--host", help="address to bind [FSWIKI_HOST]")
    p.add_argument("--port", type=int, help="port to bind [FSWIKI_PORT]")
    p.add_argument("--backend", help="which render backend to use")
    p.add_argument("--no-migrate", action="store_true",
                   help="assume the schema is already loaded")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"fswiki-serve: {exc}", file=sys.stderr)
        return 2
    if args.host:
        config = replace(config, host=args.host)
    if args.port:
        config = replace(config, port=args.port)

    if not args.no_migrate:
        try:
            result = migrate(config)
        except MigrationError as exc:
            print(f"fswiki-serve: {exc}", file=sys.stderr)
            return 1
        log.info("schema: %s", "already present" if result.already_present
                 else f"loaded {len(result.loaded)} files")

    try:
        postgrest = Postgrest(config)
        postgrest.start()
    except PostgrestError as exc:
        print(f"fswiki-serve: {exc}", file=sys.stderr)
        return 1

    # Imported here rather than at the top: uvicorn is only needed to serve,
    # and a failure in either phase above should not be preceded by the cost of
    # importing an HTTP server it will never reach.
    import uvicorn

    app = Application(config, backend=args.backend)
    log.info("serving on http://%s:%s/", config.host, config.port)
    log.info("postgrest is on %s, and clients talk to it directly",
             config.postgrest_url)
    try:
        uvicorn.run(app, host=config.host, port=config.port,
                    log_level="warning", access_log=False,
                    # Both optional and both present in the packaged build;
                    # uvicorn falls back on its pure-python versions if not.
                    loop="uvloop", http="httptools")
    finally:
        postgrest.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
