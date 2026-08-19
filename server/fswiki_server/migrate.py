"""Getting the schema into the database, before anything is served.

The first of the three startup phases -- migrate, start PostgREST, serve --
and the order is not a preference. PostgREST builds a schema cache when it
connects, so starting it against a database that is about to change gets a
cache of the old shape and 404s on everything new.

The schema is in three directories, and the split is the whole design.

**tables/** is state. Tables, columns, types, indexes, and which tables have
RLS enabled. There is exactly one path from what a table holds today to what
it should hold tomorrow, so this is the half that needs an ordered, append-only
chain -- and the half that must never be re-run over a database that already
has it, because `create table if not exists` silently ignores a definition that
has changed since.

**runtime/** is not state. 54 functions, 2 views, 24 policies, 18 triggers and
every grant: objects with no contents of their own, whose definition is the
file. Last-write-wins is not a compromise for these, it is correct. So they are
dropped and replayed on every start, and the repository is the only place they
are described. That is already how they were written -- every function in this
schema is `create or replace`, and re-running them over a live database was
measured to produce zero errors.

**seed/** is the handful of rows the wiki cannot work without: the built-in
roles, the `public` group, the root of the tree. Replayed every start like the
runtime half, because every statement is idempotent by construction, and it is
replayed *after* it because the root document's path is computed by a trigger.
Load it earlier and root arrives with a null path.

Three consequences worth naming.

A change to a function is a change to one file, with no migration to write.
A change to a table is a migration, and cannot be smuggled in as a redefinition.
And the runtime half is dropped and rebuilt inside one transaction, so there is
no instant at which the database is half of one version and half of another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .config import MIGRATION_LOCK, Config

log = logging.getLogger(__name__)

TRACKS = ("tables", "runtime", "seed")

# Order matters and is the reverse of the dependency order.
#
# Policies and triggers first, because they are attached to tables and hold
# functions up. Then functions, with CASCADE -- they call each other, and a
# flat list would fail on whichever happened to be dropped first. Then views,
# *after* functions and not before, because two functions in
# 100_impersonation.sql are declared `returns setof wiki.syncable_document`
# and hold the view up until they are gone.
#
# CASCADE is safe here, and it is checked rather than assumed: nothing outside
# this set depends on a wiki function. No index, no constraint, no column
# default. test_server_migrate.py asserts that, because the day someone adds
# `check (wiki.is_slug(name))` to a table, this line quietly starts dropping
# constraints.
DROP_RUNTIME = """
do $$
declare stmt text;
begin
  for stmt in
    select format('drop policy if exists %I on %I.%I;',
                  policyname, schemaname, tablename)
      from pg_policies where schemaname = 'wiki'
    union all
    select format('drop trigger if exists %I on %I.%I;',
                  t.tgname, n.nspname, c.relname)
      from pg_trigger t
      join pg_class c on c.oid = t.tgrelid
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'wiki' and not t.tgisinternal
  loop
    execute stmt;
  end loop;

  for stmt in
    select format('drop function if exists %I.%I(%s) cascade;',
                  n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'wiki'
  loop
    execute stmt;
  end loop;

  for stmt in
    select format('drop view if exists %I.%I cascade;', schemaname, viewname)
      from pg_views where schemaname = 'wiki'
  loop
    execute stmt;
  end loop;
end
$$;
"""


class MigrationError(Exception):
    """The schema could not be brought up to date."""


@dataclass(frozen=True)
class Migration:
    """What the phase did, for the caller to log and the tests to assert on."""
    tables: tuple[str, ...]
    runtime: tuple[str, ...]
    seed: tuple[str, ...]
    waited: bool

    @property
    def created(self) -> bool:
        """Whether this was an empty database a moment ago."""
        return bool(self.tables)


def schema_files(schema_dir: Path, track: str) -> list[Path]:
    """One track's .sql files, in load order.

    Sorted by name, which is what the numeric prefixes are for. In runtime/,
    950_lockdown must be last: PostgreSQL makes every new function executable
    by PUBLIC and offers no way to create one that is not, so the revoke has to
    follow the final create.
    """
    directory = schema_dir / track
    if not directory.is_dir():
        raise MigrationError(f"no {track}/ in {schema_dir}")
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise MigrationError(f"no .sql files in {directory}")
    return files


def _run(conn: psycopg.Connection, path: Path) -> None:
    log.debug("loading %s/%s", path.parent.name, path.name)
    try:
        conn.execute(path.read_text())
    except psycopg.Error as exc:
        raise MigrationError(f"{path.parent.name}/{path.name}: {exc}") from exc


def migrate(config: Config) -> Migration:
    """Bring the database to the schema in the repository.

    Safe to call from two processes at once, and safe to call on every start.
    """
    tracks = {t: schema_files(config.schema_dir, t) for t in TRACKS}

    try:
        conn = psycopg.connect(config.database_url)
    except psycopg.OperationalError as exc:
        # The database itself is not ours to create: this program is handed a
        # URL, and a program that creates the database it was pointed at is a
        # program that quietly papers over a typo in the URL.
        raise MigrationError(f"cannot reach {config.database_url}: {exc}") from exc

    with conn:
        # The lock is taken outside the work below and released when this
        # connection closes, including when it closes because the process died
        # mid-load. Two servers starting at once -- a restart overlapping its
        # predecessor, a rolling deploy, or someone running it twice -- would
        # otherwise both find the schema absent and both start loading it.
        waited = not conn.execute(
            "select pg_try_advisory_lock(%s)", (MIGRATION_LOCK,)).fetchone()[0]
        if waited:
            log.info("another process is migrating; waiting for it")
            conn.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        conn.commit()

        present = conn.execute(
            "select exists (select 1 from pg_namespace where nspname = 'wiki')"
        ).fetchone()[0]

        loaded_tables: list[str] = []
        if not present:
            log.info("empty database: loading %d table files", len(tracks["tables"]))
            for path in tracks["tables"]:
                _run(conn, path)
                loaded_tables.append(path.name)
        else:
            # Where the ordered chain will go. Until it exists, an existing
            # database keeps the tables it has, and a change to them is not
            # deployable -- which is a missing feature rather than a silent
            # one, and better said here than discovered later.
            log.info("tables already present; leaving them alone")

        log.info("rebuilding the runtime half")
        conn.execute(DROP_RUNTIME)
        for path in tracks["runtime"]:
            _run(conn, path)
        for path in tracks["seed"]:
            _run(conn, path)

        # One transaction over the drop and the replay, so there is no instant
        # at which the schema is half of one version and half of another --
        # and so a failure anywhere in it leaves the database as it was.
        conn.commit()

    return Migration(
        tables=tuple(loaded_tables),
        runtime=tuple(p.name for p in tracks["runtime"]),
        seed=tuple(p.name for p in tracks["seed"]),
        waited=waited,
    )
