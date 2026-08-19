"""The in-database suite, run from here so that one command runs everything.

`server/test/*.sql` asserts the things that are only true *inside* a
transaction: RLS policies filtering rather than raising, `security definer`
functions not becoming a second way to read, a `WITH CHECK` refusing a forged
author. None of that is reachable over HTTP, because by the time a request has
an answer the transaction is over.

It runs in a **database of its own** on the same cluster. The tests publish
revisions, insert drafts and leave temp tables behind, and sharing a database
with the HTTP suite would make each one's results depend on when the other
happened to run. A second schema load costs a second or two; a suite whose
failures depend on ordering costs an afternoon.

`server/test/run.sh` still exists and does the same thing against a cluster of
its own, for working on the SQL without a Python toolchain in the way.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import ROOT, _load

SQL_DB = "fswiki_sql"


@pytest.fixture(scope="session")
def sql_results(stack):
    """Load the schema and the suite into a fresh database; return the rows.

    One fixture rather than one per file: the files are ordered and share
    state — 010_fixtures builds the tree the rest assert against — so running
    one of them alone would not mean anything.
    """
    subprocess.run(["createdb", "-h", "127.0.0.1", "-p", str(stack.pg_port),
                    "-U", "postgres", SQL_DB], check=True, capture_output=True)

    for path in sorted((ROOT / "server" / "sql").glob("*.sql")):
        _load(stack.pg_port, path, db=SQL_DB)
    for path in sorted((ROOT / "server" / "test").glob("0*.sql")):
        _load(stack.pg_port, path, db=SQL_DB)

    raw = stack.psql(
        "select coalesce(jsonb_agg(to_jsonb(r) order by r.seq), '[]') "
        "from wiki_test.result r", db=SQL_DB)
    return json.loads(raw)


def test_every_assertion_in_the_sql_suite_passes(sql_results):
    """Reported as one test with every failure named, which is how the SQL
    harness itself works: results are collected rather than raised, so a run
    reports all of them instead of stopping at the first."""
    failed = [r for r in sql_results if not r["ok"]]
    assert not failed, "\n".join(
        f"  {r['label']}: {r['detail']}" for r in failed)


def test_the_suite_actually_ran(sql_results):
    """The harness records results by inserting rows. A file that failed to
    load, or a `security definer` that stopped being one, produces no rows at
    all — and an empty result set would otherwise read as a clean run."""
    assert len(sql_results) > 100, f"only {len(sql_results)} assertions ran"
