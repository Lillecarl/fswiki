"""fswiki-serve's own argument handling and its exit codes.

The end-to-end test in test_server_app.py runs the real binary and proves the
three phases work together. It cannot say anything about the paths where a
phase *fails*, because it is a subprocess that is SIGTERMed at the end and
writes no coverage. These call main() in-process, and every one of them stops
before uvicorn.

Exit codes are the interface a supervisor reads: 2 for "you configured this
wrong" and 1 for "the configuration was fine and it did not work", which are
different problems for whoever is on call.
"""

from __future__ import annotations

import pytest

from conftest import ROOT
from fswiki_server.__main__ import main, parse_args


def test_the_defaults_are_all_none_so_the_environment_wins():
    """Every flag overrides the environment, so an unset flag has to be
    distinguishable from a flag set to the default."""
    args = parse_args([])
    assert args.host is None and args.port is None and args.backend is None
    assert not args.no_migrate


def test_flags_are_parsed():
    args = parse_args(["--host", "0.0.0.0", "--port", "9", "--no-migrate", "-v"])
    assert (args.host, args.port, args.no_migrate, args.verbose) == \
        ("0.0.0.0", 9, True, True)


def test_no_database_url_is_a_configuration_error(monkeypatch, capsys):
    monkeypatch.delenv("FSWIKI_DATABASE_URL", raising=False)
    assert main([]) == 2
    assert "FSWIKI_DATABASE_URL" in capsys.readouterr().err


def test_a_database_it_cannot_reach_fails_before_anything_is_bound(
        monkeypatch, capsys):
    """1 rather than 2: nothing was configured wrongly, it just is not there."""
    monkeypatch.setenv("FSWIKI_DATABASE_URL",
                       "postgres://postgres@127.0.0.1:1/nothing")
    monkeypatch.setenv("FSWIKI_SCHEMA_DIR", str(ROOT / "server" / "schema"))
    assert main([]) == 1
    assert "cannot reach" in capsys.readouterr().err


def test_a_postgrest_it_cannot_run_fails_before_anything_is_bound(
        monkeypatch, capsys, stack):
    """--no-migrate, so this is the second phase failing on its own. The
    schema is already loaded here; skipping the first phase is what isolates
    the second."""
    monkeypatch.setenv("FSWIKI_DATABASE_URL",
                       f"postgres://postgres@127.0.0.1:{stack.pg_port}/fswiki")
    monkeypatch.setenv("FSWIKI_SCHEMA_DIR", str(ROOT / "server" / "schema"))
    monkeypatch.setenv("FSWIKI_POSTGREST_BIN", "postgrest-that-is-not-installed")
    assert main(["--no-migrate"]) == 1
    assert "FSWIKI_POSTGREST_BIN" in capsys.readouterr().err
