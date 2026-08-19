"""The startup phase that puts PostgREST behind us.

It is a child process rather than a sibling under a supervisor for one
concrete reason: PostgREST builds its schema cache when it connects and does
not notice DDL afterwards, so whatever changes the schema has to be able to
tell it. Being its parent makes that a signal rather than a NOTIFY channel
with configuration at both ends.

These start a second PostgREST against the session's database, on a port of
its own. The session's own is left alone: killing it would take every later
test with it.
"""

from __future__ import annotations

import socket
import subprocess

import pytest

from conftest import ROOT, wait_for
from fswiki_server.config import Config
from fswiki_server.postgrest import Postgrest, PostgrestError, answers, environment

SCHEMA = ROOT / "server" / "schema"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def config(stack):
    return Config(
        database_url=f"postgres://postgres@127.0.0.1:{stack.pg_port}/fswiki",
        schema_dir=SCHEMA,
        postgrest_port=free_port(),
        postgrest_jwt_secret=stack.secret,
    )


# --- the configuration, without running anything ----------------------------

def test_the_anonymous_role_and_the_hook_are_both_set(config):
    """Two settings here are security rather than plumbing. Without the anon
    role an unauthenticated request has no role to be; without the pre-request
    hook the impersonation headers are silently ignored, which fails open."""
    env = environment(config)
    assert env["PGRST_DB_ANON_ROLE"] == "fswiki_anon"
    assert env["PGRST_DB_PRE_REQUEST"] == "wiki.pre_request"
    assert env["PGRST_DB_SCHEMAS"] == "wiki"


def test_it_connects_with_the_postgrest_url_when_there_is_one(stack):
    """The migration URL needs DDL rights and this one must not have them. A
    PostgREST connected as the owner of the tables would bypass every policy in
    050_rls.sql, because owners are not subject to RLS."""
    cfg = Config(
        database_url="postgres://owner@example.invalid/fswiki",
        postgrest_database_url=f"postgres://fswiki_authenticator@127.0.0.1:"
                               f"{stack.pg_port}/fswiki",
        schema_dir=SCHEMA,
    )
    assert "fswiki_authenticator" in environment(cfg)["PGRST_DB_URI"]


# --- running it -------------------------------------------------------------

def test_start_does_not_return_until_it_is_answering(config):
    """Returning early would hand the serve phase a socket nothing is
    listening on, and the first visitor would see a connection refused that
    looks like a bug rather than a race."""
    with Postgrest(config) as pg:
        assert pg.running
        assert answers(pg.url)


def test_it_serves_the_schema_it_was_pointed_at(config, rest):
    """Not just answering: answering about this wiki. A PostgREST with an empty
    schema cache also answers on /, which is why readiness alone is not
    enough of an assertion."""
    from conftest import http
    with Postgrest(config) as pg:
        r = http(f"{pg.url}/syncable_document?select=id&limit=1",
                 token=config.postgrest_jwt_secret and None)
        # Anonymous: fswiki_anon may select the view, and sees only what was
        # granted to public -- which in these fixtures is nothing. An empty
        # list is the right answer and a 404 would mean the schema cache is.
        assert r.code == 200, r.body


def test_stopping_it_stops_it(config):
    pg = Postgrest(config)
    pg.start()
    url = pg.url
    pg.stop()
    assert not pg.running
    assert not answers(url)


def test_stop_is_safe_when_it_never_started(config):
    Postgrest(config).stop()


def test_starting_twice_is_refused_rather_than_leaking_a_process(config):
    with Postgrest(config) as pg:
        with pytest.raises(PostgrestError, match="already started"):
            pg.start()


def test_reload_needs_something_to_reload(config):
    with pytest.raises(PostgrestError, match="not running"):
        Postgrest(config).reload()


def test_it_notices_a_function_added_after_it_connected(config, stack):
    """The reason it is a child process at all. PostgREST builds its schema
    cache on connect and does not notice DDL, so a function created afterwards
    is a 404 until it is told -- and the two-track migration plan reloads the
    whole runtime half of the schema on every deploy."""
    from conftest import http
    with Postgrest(config) as pg:
        stack.exec("""
            create or replace function wiki.reload_canary()
            returns integer language sql stable as $$ select 42 $$;
            grant execute on function wiki.reload_canary() to fswiki_anon;
        """)
        try:
            before = http(f"{pg.url}/rpc/reload_canary", method="POST", body={})
            assert before.code == 404, (
                f"expected a stale schema cache, got {before.code}")

            # SIGUSR1 reloads the schema cache; SIGUSR2 reloads the config
            # and would leave this 404 forever. The reload is asynchronous, so
            # poll rather than assume it landed before the next request.
            pg.reload()

            def reloaded():
                r = http(f"{pg.url}/rpc/reload_canary", method="POST", body={})
                return r if r.code == 200 else None

            after = wait_for(reloaded, what="the reloaded schema cache")
            assert after.body.strip() == "42"
        finally:
            stack.exec("drop function if exists wiki.reload_canary();")


# --- failing usefully -------------------------------------------------------

def test_a_database_it_cannot_reach_is_an_error_not_a_hang(stack):
    cfg = Config(
        database_url="postgres://postgres@127.0.0.1:1/nothing",
        schema_dir=SCHEMA,
        postgrest_port=free_port(),
    )
    with pytest.raises(PostgrestError):
        Postgrest(cfg).start(timeout=8)


def test_answers_says_no_rather_than_raising_when_nothing_is_there():
    """A refused connection is not a response. A caller waiting for readiness
    needs 'not yet' and a caller checking health needs 'no'; conflating them
    turns a server that never started into a server that returned an error."""
    assert answers(f"http://127.0.0.1:{free_port()}/", timeout=0.5) is False


def test_a_postgrest_that_is_not_installed_says_which_binary(stack):
    """The commonest deployment mistake. A bare FileNotFoundError out of Popen
    names the binary without saying what wanted it or how to fix it."""
    cfg = Config(
        database_url=f"postgres://postgres@127.0.0.1:{stack.pg_port}/fswiki",
        schema_dir=SCHEMA,
        postgrest_bin="postgrest-that-is-not-installed",
        postgrest_port=free_port(),
    )
    with pytest.raises(PostgrestError, match="FSWIKI_POSTGREST_BIN"):
        Postgrest(cfg).start(timeout=5)
