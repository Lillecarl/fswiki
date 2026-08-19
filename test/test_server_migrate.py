"""The startup phase that puts the schema in the database.

The schema is in three directories and the split is the design. `tables/` is
state and loads once. `runtime/` -- functions, views, policies, triggers,
grants -- is dropped and replayed on every start, because those objects have no
contents of their own and the file *is* the definition. `seed/` is the handful
of rows the wiki cannot work without, replayed after runtime because the root
document's path is computed by a trigger.

Two properties make that safe, and both are asserted below rather than argued:
a rebuild produces exactly the schema a fresh load produces, and it does not
touch a single row.
"""

from __future__ import annotations

import subprocess
import threading

import psycopg
import pytest

from conftest import ROOT
from fswiki_server.config import MIGRATION_LOCK, Config, ConfigError
from fswiki_server.migrate import (TRACKS, MigrationError, migrate,
                                   schema_files)

SCHEMA = ROOT / "server" / "schema"


def url_for(stack, db: str) -> str:
    return f"postgres://postgres@127.0.0.1:{stack.pg_port}/{db}"


def dump(stack, db: str) -> str:
    """The schema as pg_dump sees it, which is the only opinion that counts.

    PostgreSQL 18 stamps a random \\restrict nonce into every dump; without
    filtering it, no two dumps of anything are ever equal.
    """
    out = subprocess.run(
        ["pg_dump", "-h", "127.0.0.1", "-p", str(stack.pg_port), "-U", "postgres",
         "--schema-only", "--no-owner", "--schema=wiki", db],
        check=True, capture_output=True, text=True).stdout
    return "\n".join(l for l in out.splitlines()
                     if l.strip() and not l.startswith(("--", "\\restrict",
                                                        "\\unrestrict")))


@pytest.fixture
def blank(stack):
    """A database of its own, dropped afterwards."""
    name = "fswiki_migrate_test"

    def drop():
        subprocess.run(["dropdb", "--if-exists", "-h", "127.0.0.1",
                        "-p", str(stack.pg_port), "-U", "postgres", name],
                       check=False, capture_output=True)

    drop()
    subprocess.run(["createdb", "-h", "127.0.0.1", "-p", str(stack.pg_port),
                    "-U", "postgres", name], check=True, capture_output=True)
    yield Config(database_url=url_for(stack, name), schema_dir=SCHEMA)
    drop()


# --- the three tracks -------------------------------------------------------

@pytest.mark.parametrize("track", TRACKS)
def test_every_track_has_files(track):
    assert schema_files(SCHEMA, track)


def test_the_lockdown_is_the_last_thing_in_the_runtime_track():
    """PostgreSQL makes every new function executable by PUBLIC and offers no
    way to create one that is not, so the revoke has to follow the final
    create. Sorted order enforces it; the numeric prefixes are why sorting
    works."""
    assert schema_files(SCHEMA, "runtime")[-1].name == "950_lockdown.sql"


def test_a_missing_track_is_an_error_that_names_it(tmp_path):
    with pytest.raises(MigrationError, match="tables/"):
        schema_files(tmp_path, "tables")


def test_no_table_object_is_defined_in_the_runtime_track():
    """The split has to stay split. A `create table` that drifts into runtime/
    would be dropped and recreated on every deploy, which is a way to lose a
    table's contents on a Tuesday."""
    for path in schema_files(SCHEMA, "runtime"):
        body = path.read_text().lower()
        for forbidden in ("\ncreate table", "\ncreate type", "\ncreate index",
                          "\ncreate unique index"):
            assert forbidden not in body, f"{path.name} defines {forbidden.strip()}"


# --- loading ----------------------------------------------------------------

def test_a_blank_database_gets_all_three_tracks(blank):
    result = migrate(blank)
    assert result.created
    assert result.tables == tuple(p.name for p in schema_files(SCHEMA, "tables"))
    assert result.runtime == tuple(p.name for p in schema_files(SCHEMA, "runtime"))
    assert result.seed == tuple(p.name for p in schema_files(SCHEMA, "seed"))


def test_and_the_schema_is_usable_afterwards(blank):
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        assert c.execute("select count(*) from wiki.principal where name = 'public'"
                         ).fetchone()[0] == 1
        assert c.execute("select count(*) from pg_proc p join pg_namespace n "
                         "on n.oid = p.pronamespace where n.nspname = 'wiki'"
                         ).fetchone()[0] > 50
        assert c.execute("select count(*) from pg_policies where schemaname = 'wiki'"
                         ).fetchone()[0] > 20


def test_the_root_document_gets_its_path_from_the_trigger(blank):
    """Why seed/ loads after runtime/ and not with the tables. wiki.document's
    path is computed by document_path_sync; seed the root before that trigger
    exists and root arrives with a null path and the whole tree is unreachable."""
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        assert c.execute("select path::text from wiki.document where slug = 'root'"
                         ).fetchone()[0] == "root"


# --- rebuilding -------------------------------------------------------------

def test_a_rebuild_produces_exactly_what_a_fresh_load_produces(blank, stack):
    """The property the whole split rests on. If replaying runtime/ over an
    existing database differed from loading it into an empty one -- by a
    function, a policy expression, a trigger or a grant -- then production and
    the test suite would be running different schemas."""
    migrate(blank)
    fresh = dump(stack, "fswiki_migrate_test")
    migrate(blank)
    assert dump(stack, "fswiki_migrate_test") == fresh


def test_a_rebuild_does_not_touch_a_single_row(blank):
    """Tables are not in the runtime track, so nothing here should reach them.
    Asserted with a row that was not seeded, because seed data would be put
    back by the rebuild and prove nothing."""
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        c.execute("insert into wiki.principal (kind, name) values ('user', 'canary')")
        before = c.execute("select count(*) from wiki.document").fetchone()[0]
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        assert c.execute("select count(*) from wiki.principal where name = 'canary'"
                         ).fetchone()[0] == 1
        assert c.execute("select count(*) from wiki.document").fetchone()[0] == before


def test_a_function_no_longer_in_the_files_is_gone_after_a_rebuild(blank):
    """The reason to drop rather than replace. `create or replace` leaves
    behind whatever the files no longer mention, so a function deleted in the
    repository would stay callable in production forever."""
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        c.execute("create function wiki.left_behind() returns int "
                  "language sql as $$ select 1 $$")
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        assert c.execute(
            "select count(*) from pg_proc p join pg_namespace n "
            "on n.oid = p.pronamespace "
            "where n.nspname = 'wiki' and p.proname = 'left_behind'"
        ).fetchone()[0] == 0


def test_tables_are_not_reloaded_over_a_database_that_has_them(blank):
    """`create table if not exists` silently ignores a definition that has
    changed, so re-running the table track would look like it worked and leave
    the column you added missing. It is skipped, and says so."""
    migrate(blank)
    second = migrate(blank)
    assert not second.created and second.tables == ()
    assert second.runtime and second.seed


def test_dropping_the_runtime_half_reaches_no_table_object(blank):
    """`drop function ... cascade` is what makes the rebuild possible, and it
    is only safe while nothing outside the runtime half depends on a wiki
    function. The day someone writes `check (wiki.is_slug(name))` on a table,
    cascade starts dropping constraints and this fails instead."""
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        rows = c.execute("""
            select 'index '||i.indexrelid::regclass::text
              from pg_index i
              join pg_depend d on d.objid = i.indexrelid
                               and d.refclassid = 'pg_proc'::regclass
              join pg_proc p on p.oid = d.refobjid
              join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'wiki'
            union all
            select 'constraint '||c2.conname
              from pg_constraint c2
              join pg_depend d on d.objid = c2.oid
                               and d.refclassid = 'pg_proc'::regclass
              join pg_proc p on p.oid = d.refobjid
              join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'wiki'
            union all
            select 'default on '||a.adrelid::regclass::text
              from pg_attrdef a
              join pg_depend d on d.objid = a.oid
                               and d.refclassid = 'pg_proc'::regclass
              join pg_proc p on p.oid = d.refobjid
              join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'wiki'
        """).fetchall()
    assert rows == [], f"cascade would reach: {rows}"


# --- the lock ---------------------------------------------------------------

def test_a_second_migration_waits_rather_than_racing(blank):
    """Two servers starting at once would otherwise both find the schema
    absent and both start loading it."""
    holder = psycopg.connect(blank.database_url, autocommit=True)
    holder.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK,))

    done = threading.Event()
    result = {}

    def run():
        result["migration"] = migrate(blank)
        done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert not done.wait(timeout=1.5), "migrate ran while the lock was held"

    holder.close()
    assert done.wait(timeout=60), "migrate never finished after the lock was freed"
    worker.join(timeout=5)
    assert result["migration"].waited and result["migration"].created


def test_the_lock_is_released_when_the_migration_is_done(blank):
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        assert c.execute("select pg_try_advisory_lock(%s)",
                         (MIGRATION_LOCK,)).fetchone()[0] is True


# --- saying what is wrong ---------------------------------------------------

def test_a_database_that_is_not_there_names_the_url(stack):
    cfg = Config(database_url=url_for(stack, "no_such_database"), schema_dir=SCHEMA)
    with pytest.raises(MigrationError, match="no_such_database"):
        migrate(cfg)


def test_the_database_url_is_required():
    with pytest.raises(ConfigError, match="FSWIKI_DATABASE_URL"):
        Config.from_env({})


def test_the_environment_is_read_whole(stack):
    cfg = Config.from_env({
        "FSWIKI_DATABASE_URL": url_for(stack, "fswiki"),
        "FSWIKI_SCHEMA_DIR": str(SCHEMA),
        "FSWIKI_PORT": "9001",
        "FSWIKI_POSTGREST_PORT": "9002",
    })
    assert cfg.port == 9001
    assert cfg.postgrest_url == "http://127.0.0.1:9002"
    assert cfg.schema_dir == SCHEMA


# --- the chain --------------------------------------------------------------
#
# What migrate() could not do until there was a ledger: change a table on a
# database that already exists. These use a copy of the schema so a test can
# add a migration to it without editing the repository.

@pytest.fixture
def copied(blank, tmp_path):
    """The real schema, somewhere a test may add a file to it."""
    import shutil
    target = tmp_path / "schema"
    shutil.copytree(SCHEMA, target)
    return Config(database_url=blank.database_url, schema_dir=target)


def test_a_new_table_migration_reaches_a_database_that_already_exists(copied):
    """The whole point. Before the ledger this was impossible: the table track
    ran only against an empty database, so a column added to the repository
    never arrived anywhere that mattered."""
    migrate(copied)
    (copied.schema_dir / "tables" / "110_canary.sql").write_text(
        "alter table wiki.document add column canary text;\n")

    result = migrate(copied)
    assert result.tables == ("110_canary.sql",)
    with psycopg.connect(copied.database_url, autocommit=True) as c:
        assert c.execute(
            "select count(*) from information_schema.columns "
            "where table_schema='wiki' and table_name='document' "
            "and column_name='canary'").fetchone()[0] == 1


def test_and_runs_exactly_once_however_often_it_starts(copied):
    """`alter table ... add column` is not idempotent, so a chain that replayed
    would fail on the second start -- which is the failure you want, and still
    a failure."""
    migrate(copied)
    (copied.schema_dir / "tables" / "110_canary.sql").write_text(
        "alter table wiki.document add column canary text;\n")
    migrate(copied)
    assert migrate(copied).tables == ()
    assert migrate(copied).tables == ()


def test_the_ledger_records_what_ran(copied):
    migrate(copied)
    with psycopg.connect(copied.database_url, autocommit=True) as c:
        recorded = {r[0] for r in c.execute(
            "select filename from wiki.schema_migration").fetchall()}
    assert recorded == {p.name for p in schema_files(copied.schema_dir, "tables")}


def test_a_failing_migration_is_not_recorded_as_having_run(copied):
    """Recorded in the same transaction that runs it, so a failure halfway
    leaves neither the change nor the claim that it happened."""
    migrate(copied)
    (copied.schema_dir / "tables" / "110_broken.sql").write_text(
        "alter table wiki.document add column ok text;\n"
        "alter table wiki.nonexistent add column bad text;\n")

    with pytest.raises(MigrationError, match="110_broken"):
        migrate(copied)

    with psycopg.connect(copied.database_url, autocommit=True) as c:
        assert c.execute("select count(*) from wiki.schema_migration "
                         "where filename = '110_broken.sql'").fetchone()[0] == 0
        assert c.execute(
            "select count(*) from information_schema.columns "
            "where table_schema='wiki' and table_name='document' "
            "and column_name='ok'").fetchone()[0] == 0, "half a migration landed"


def test_a_database_from_before_the_ledger_is_baselined(blank, stack):
    """Every database that exists as this lands has the tables and no record of
    how it got them. The only sane reading is that it matches the files, so
    they are recorded without being run -- loudly, because it is a guess."""
    for path in schema_files(SCHEMA, "tables"):
        subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", str(stack.pg_port), "-U", "postgres",
             "-d", "fswiki_migrate_test", "-v", "ON_ERROR_STOP=1", "-X", "-q",
             "-f", str(path)], check=True, capture_output=True)

    result = migrate(blank)
    assert result.baselined == tuple(p.name for p in schema_files(SCHEMA, "tables"))
    assert result.tables == () and not result.created
    # And the runtime half still went on, which is what makes the database
    # usable rather than merely recorded.
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        assert c.execute("select count(*) from wiki.role").fetchone()[0] == 7
