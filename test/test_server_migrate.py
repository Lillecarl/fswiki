"""The startup phase that puts the schema in the database.

Three phases start this server -- migrate, start PostgREST, serve -- and the
order is load-bearing rather than tidy: PostgREST builds a schema cache when it
connects, so starting it against a database that is about to change gets a
cache of the old shape.

What migrate() does today is load the schema if it is absent and nothing if it
is present. There is no migration chain yet, so there is no path from an old
schema to a new one. The lock is what makes even that safe to run twice.
"""

from __future__ import annotations

import subprocess
import threading

import psycopg
import pytest

from conftest import ROOT
from fswiki_server.config import MIGRATION_LOCK, Config, ConfigError
from fswiki_server.migrate import MigrationError, migrate, schema_files

SCHEMA = ROOT / "server" / "schema"


def url_for(stack, db: str) -> str:
    return f"postgres://postgres@127.0.0.1:{stack.pg_port}/{db}"


@pytest.fixture
def blank(stack):
    """A database of its own, dropped afterwards.

    Migrating into the session's own database would prove nothing -- the schema
    is already there -- and migrating into it if it were not would take every
    other test with it.
    """
    name = "fswiki_migrate_test"

    def psqlrun(*args):
        subprocess.run([*args, "-h", "127.0.0.1", "-p", str(stack.pg_port),
                        "-U", "postgres"], check=False, capture_output=True)

    psqlrun("dropdb", "--if-exists", name)
    subprocess.run(["createdb", "-h", "127.0.0.1", "-p", str(stack.pg_port),
                    "-U", "postgres", name], check=True, capture_output=True)
    yield Config(database_url=url_for(stack, name), schema_dir=SCHEMA)
    psqlrun("dropdb", "--if-exists", name)


# --- the files --------------------------------------------------------------

def test_the_lockdown_is_loaded_last():
    """PostgreSQL makes every new function executable by PUBLIC and offers no
    way to create one that is not, so the revoke has to follow the final
    create. Sorted order is what enforces that, and the numeric prefixes are
    why sorted order works."""
    assert schema_files(SCHEMA)[-1].name == "950_lockdown.sql"


def test_an_empty_directory_is_an_error_not_an_empty_success(tmp_path):
    with pytest.raises(MigrationError, match="no .sql files"):
        schema_files(tmp_path)


# --- loading ----------------------------------------------------------------

def test_a_blank_database_gets_every_file(blank):
    result = migrate(blank)
    assert result.loaded == tuple(p.name for p in schema_files(SCHEMA))
    assert not result.already_present


def test_and_the_schema_is_actually_usable_afterwards(blank):
    migrate(blank)
    with psycopg.connect(blank.database_url, autocommit=True) as c:
        # The public group, the ACL walk and a policy: one object from each of
        # the three kinds the load has to get right.
        assert c.execute("select count(*) from wiki.principal where name = 'public'"
                         ).fetchone()[0] == 1
        assert c.execute("select count(*) from pg_proc p join pg_namespace n "
                         "on n.oid = p.pronamespace where n.nspname = 'wiki'"
                         ).fetchone()[0] > 50
        assert c.execute("select count(*) from pg_policies where schemaname = 'wiki'"
                         ).fetchone()[0] > 20


def test_running_it_twice_does_nothing_the_second_time(blank):
    """Which is the whole reason it is safe to call on every start."""
    migrate(blank)
    second = migrate(blank)
    assert second.already_present and second.loaded == ()


# --- the lock ---------------------------------------------------------------

def test_a_second_migration_waits_rather_than_racing(blank):
    """Two servers starting at once would otherwise both find the schema
    absent and both start loading it. Held here by a connection of our own so
    the wait is observable; in the real case the other holder is another
    process doing exactly this."""
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
    assert done.wait(timeout=30), "migrate never finished after the lock was freed"
    worker.join(timeout=5)
    assert result["migration"].waited
    assert result["migration"].loaded


def test_the_lock_is_released_when_the_migration_is_done(blank):
    """A session-level lock, so it goes when the connection does — including
    when the connection goes because the process died mid-load."""
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
