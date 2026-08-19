# The test suite

One command runs everything:

    nix run --file . tests

That builds a Postgres cluster, loads the schema, starts PostgREST in front of
it, and hands the whole thing to pytest. Nothing here talks to `fswiki-dev` —
that is somebody's working state, and a suite whose results depend on what they
had been doing is not a suite.

Arguments go through to pytest:

    nix run --file . tests -- test/test_merge.py -x
    nix run --file . tests -- -k impersonation -q

## Mounting needs a namespace

FUSE tests need a mount namespace of their own, and Postgres refuses to run as
root. Both at once:

    unshare --user --map-auto --map-root-user --mount --propagation private \
        $(nix build --file . tests --no-link --print-out-paths)/bin/fswiki-test

The three flags are each load-bearing, and the combination is the only one that
satisfies both requirements:

- `--user --map-root-user` makes us root inside the namespace, which is what
  lets us mount at all.
- `--map-auto` additionally maps the subuid range from `/etc/subuid`, so
  ordinary uids exist inside the namespace. Without it there is exactly one uid
  in there — root — and `initdb` refuses to run as root, full stop. The suite
  drops to uid 1000 for `initdb` and `pg_ctl` (see `PG_UID` in `conftest.py`).
- `--mount --propagation private` keeps the mounts out of the host's namespace,
  so a suite that dies mid-run leaves nothing behind for `ls` to hang on.

Without the namespace everything except the mount tests still runs:

    nix run --file . tests -- -m 'not mount'

The mount tests are marked, and they are marked precisely so that this works.

## What is where

| file | what it covers |
| --- | --- |
| `test_render.py` | the render seam, against every registered backend |
| `test_backends.py` | the client under both asyncio and trio |
| `test_client.py` | `fswiki_core.client` against real PostgREST |
| `test_sql.py` | the in-database suite, in a database of its own |
| `test_audit.py` | the access trail over HTTP |
| `test_impersonation.py` | `--as` / `--as-group` over HTTP |
| `test_cli.py` | `fswiki` — status, diff, push, revert, render |
| `test_merge.py` | the three-way merge, end to end through the mount |
| `test_mount.py` | the filesystem itself |
| `test_mount_audit.py` | `fswiki-mount --audit` |
| `test_mount_impersonation.py` | `fswiki-mount --as` |
| `test_preview.py` | `fswiki preview` |

## How it is put together

**One stack per session.** `initdb` is seconds and everything after it is
milliseconds, so one cluster per run is the right granularity. Per-test
isolation is the `clean` fixture, which empties drafts and events *before* each
test — before rather than after, so a failed test leaves its wreckage to be
looked at.

**Published revisions are never rolled back.** Resetting the tree would mean
rebuilding the database per test. Tests that publish are written to tolerate a
tip that has moved, which is also closer to what a real wiki does.

**Impersonation grants are configuration, not state.** `clean` leaves them
alone: a mount started with `--as` outlives the test that started it and keeps
polling, and revoking behind its back would fail some unrelated later test with
a 403 from a filesystem it never asked about.

**Never sleep; wait for the thing.** The mount polls, the audit shipper
batches, PostgREST loads a schema cache. `wait_for()` polls a predicate and
fails with what it last saw, which is both faster than sleeping for the worst
case and the only version that fails with a useful message.

**Async tests run under the anyio plugin**, not pytest-asyncio. The client is
written against anyio so that the same code runs under trio inside the FUSE
mount and under asyncio everywhere else; `test_backends.py` overrides one
fixture and runs against both, which is the whole reason to pick this plugin.

## The other suite

`server/test/run.sh` builds a cluster of its own and runs the same SQL that
`test_sql.py` runs. It exists for working on the schema without a Python
toolchain in the way; both report the same assertions.
