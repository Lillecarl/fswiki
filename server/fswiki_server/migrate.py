"""Getting the schema into the database, before anything is served.

This is the first of the three startup phases -- migrate, start PostgREST,
serve -- and the order is not a preference. PostgREST builds a schema cache
when it connects, so starting it against a database that is about to change
gets a cache of the old shape and 404s on everything new.

**What this does today is load the schema if it is absent, and nothing if it
is present.** That is the whole of it, and it is worth saying plainly rather
than dressing up: fswiki has no migration chain yet, so there is no path from
an old schema to a new one, only from an empty database to the current one.
The phase exists now, with its lock and its ordering, so that the chain has
somewhere to go when it is written -- see the two-track plan: the tables get
an ordered chain, the functions, views, policies and grants get rebuilt from
these files every start.

The lock is not optional even so. Two servers starting at once -- a restart
overlapping its predecessor, a rolling deploy, or someone running it twice --
would otherwise both find the schema absent and both start loading it. A
session-level advisory lock turns that race into a wait, and it is held for
the whole phase and released when the connection closes, including when it
closes because the process died.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .config import MIGRATION_LOCK, Config

# Synchronous, and not merely because startup can afford to block. The lock is
# session-level, so one connection has to stay open across the whole phase --
# and the phase is strictly sequential, running before there is an event loop
# to share. psycopg's AsyncConnection is in the same package for the day a
# request handler needs SQL; see the package docstring for what such a handler
# is and is not allowed to ask.

log = logging.getLogger(__name__)


class MigrationError(Exception):
    """The schema could not be brought up to date."""


@dataclass(frozen=True)
class Migration:
    """What the phase did, for the caller to log and the tests to assert on."""
    loaded: tuple[str, ...]
    waited: bool

    @property
    def already_present(self) -> bool:
        return not self.loaded


def schema_files(schema_dir: Path) -> list[Path]:
    """The .sql files, in load order.

    Sorted by name, which is what the numeric prefixes are for: 950_lockdown
    must be last because PostgreSQL makes every new function executable by
    PUBLIC and offers no way to create one that is not, so the revoke has to
    follow the final create.
    """
    files = sorted(schema_dir.glob("*.sql"))
    if not files:
        raise MigrationError(f"no .sql files in {schema_dir}")
    return files


def migrate(config: Config) -> Migration:
    """Ensure the schema is present. Safe to call from two processes at once."""
    files = schema_files(config.schema_dir)
    try:
        conn = psycopg.connect(config.database_url, autocommit=True)
    except psycopg.OperationalError as exc:
        # The database itself is not ours to create: this program is handed a
        # URL, and a program that creates the database it was pointed at is a
        # program that quietly papers over a typo in the URL.
        raise MigrationError(f"cannot reach {config.database_url}: {exc}") from exc

    with conn:
        # Non-blocking first, only so the wait can be reported rather than
        # looking like a hang. The blocking call below is what actually
        # serialises us.
        waited = not conn.execute(
            "select pg_try_advisory_lock(%s)", (MIGRATION_LOCK,)).fetchone()[0]
        if waited:
            log.info("another process is migrating; waiting for it")
            conn.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK,))

        present = conn.execute(
            "select exists (select 1 from pg_namespace where nspname = 'wiki')"
        ).fetchone()[0]
        if present:
            log.info("schema already present; nothing to do")
            return Migration(loaded=(), waited=waited)

        loaded = []
        for path in files:
            log.info("loading %s", path.name)
            try:
                conn.execute(path.read_text())
            except psycopg.Error as exc:
                raise MigrationError(f"{path.name}: {exc}") from exc
            loaded.append(path.name)
        log.info("loaded %d schema files", len(loaded))
        return Migration(loaded=tuple(loaded), waited=waited)
