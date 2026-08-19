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

## In a build sandbox

    nix build --file . tests.check -L

450 tests, about twenty seconds, entirely inside a pure Nix build. That is
every pure unit below, plus the client, the SQL suite, the audit trail over
HTTP and impersonation over HTTP. Two skip themselves there: the filesystem
under the build's `$TMPDIR` may not carry user extended attributes, and
`test_paths.py` says so rather than guessing.

Two things were measured rather than assumed. **Loopback works**: the sandbox's
`lo` is up with 127.0.0.1 and binds and connects fine, so Postgres and PostgREST
need nothing special in there. PostgREST does support Unix sockets
(`server-unix-socket`), but they would buy nothing — the network is not what is
missing.

**`/dev/fuse` is what is missing.** The sandbox's `/dev` holds null, zero,
random, tty and little else, and no amount of unsharing conjures a device node
that is not there. So the mount half cannot run in a build at all, and `-m 'not
mount'` is exactly the line between the two. The mount fixture also skips itself
when `/dev/fuse` is absent, so the marker is a convenience rather than the only
thing standing between you and a wall of confusing failures.

One environment fix worth knowing about: httpx builds a default SSL context even
for an `http://` URL, so without `SSL_CERT_FILE` every test that uses the client
dies with a bare `FileNotFoundError` out of `ssl.py`. The check derivation sets
it.

## What is where

Pure units first — no stack, no network, no mount, milliseconds each:

| file | what it covers |
| --- | --- |
| `test_naming.py` | filenames to slugs and back, which everything else assumes |
| `test_merge_algorithm.py` | the three-way merge as a function, marker growth included |
| `test_links.py` | wikilink rewriting, and forbidden ≡ missing |
| `test_paths.py` | the three ways a human names one document |
| `test_model.py` | folding drafts over the manifest into one tree |
| `test_inodes.py` | inode stability and the kernel's lookup counting |
| `test_procinfo.py` | reading /proc, and never shipping what was in argv |
| `test_report.py` | every word the CLI prints |
| `test_audit_queue.py` | the audit queue as a queue: cap, batching, recovery |
| `test_render.py` | the render seam, against every registered backend |

Then against a live stack:

| file | what it covers |
| --- | --- |
| `test_backends.py` | the client under both asyncio and trio |
| `test_client.py` | reading, and never from `current_document` |
| `test_client_drafts.py` | drafts, push, revisions and the merge RPCs |
| `test_client_transport.py` | the transport impersonation forces, and how it fails |
| `test_sql.py` | the in-database suite, in a database of its own |
| `test_audit.py` | the access trail over HTTP |
| `test_impersonation.py` | `--as` / `--as-group` over HTTP |
| `test_public.py` | what a request with no token at all can reach |
| `test_cli.py` | `fswiki` — status, diff, push, revert, render |
| `test_cli_impersonation.py` | `fswiki --as`, including that it cannot write |
| `test_preview.py` | `fswiki preview` |

And through a real mount:

| file | what it covers |
| --- | --- |
| `test_mount.py` | the filesystem itself |
| `test_merge.py` | the three-way merge, end to end through the mount |
| `test_mount_audit.py` | `fswiki-mount --audit`, including offline spooling |
| `test_mount_offline.py` | what the mount does when the server goes away |
| `test_mount_impersonation.py` | `fswiki-mount --as` |

The split is not filing. A unit test that reaches the awkward case in one dict
and an end-to-end test that proves the wiring is real are answering different
questions, and neither substitutes for the other: `test_merge.py` cannot
cheaply ask what happens to a page that already contains conflict markers, and
`test_merge_algorithm.py` cannot tell you whether the draft's `base_version`
was recorded from the revision the reader actually read.

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

**A server a test may kill.** The `spare` fixture starts a second PostgREST
against the same database, with `stop()` and `start()`. Killing the session's
own would take every later test with it, and "what happens when the server goes
away" is not answerable any other way — it is also where two of the suite's
first real bugs were.

**Short kernel TTLs, on purpose.** Mounts start with `--ttl 0.2 --poll 0.25`
rather than the shipped 5 and 2. `--ttl` is how long the kernel may answer a
lookup without asking the mount at all, so at 5 seconds every "has it noticed
yet?" costs up to five seconds of stale cache — the merge tests, which are
nothing but that question, were two thirds of the suite's wall clock. Serving a
getattr from the tree we already hold is free, and a poll is a few bytes.

**Never sleep; wait for the thing.** The mount polls, the audit shipper
batches, PostgREST loads a schema cache. `wait_for()` polls a predicate and
fails with what it last saw, which is both faster than sleeping for the worst
case and the only version that fails with a useful message.

**Async tests run under the anyio plugin**, not pytest-asyncio. The client is
written against anyio so that the same code runs under trio inside the FUSE
mount and under asyncio everywhere else; `test_backends.py` overrides one
fixture and runs against both, which is the whole reason to pick this plugin.

## Measuring it

    FSWIKI_COVERAGE=1 nix run --file . tests

Off by default, because it is a measurement rather than a test. What it exists
for is the four largest modules in the project — `fuse/fs.py`, both
`__main__.py`, and `preview.py` — which only ever run in a **subprocess**. An
ordinary in-process coverage run reports every one of them as zero however
thoroughly the mount and CLI tests exercise them, which is the same number an
untested module gets and therefore worse than no number at all.

The mode sets `COVERAGE_PROCESS_START` and puts a `sitecustomize.py` and a
`coverage` package on `PYTHONPATH`, so every child interpreter starts measuring
itself, and combines the results at the end. The CLI, the mount and the preview
server each run in their own Nix python environment, which is why `coverage`
has to be put on the path rather than assumed to be installed.

## The other suite

`server/test/run.sh` builds a cluster of its own and runs the same SQL that
`test_sql.py` runs. It exists for working on the schema without a Python
toolchain in the way; both report the same assertions.
