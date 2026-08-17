# Identifying the process behind an open

The FUSE client can name the process that opened a document. This records what
it can see, what that costs, and what the record is and is not worth.

## What FUSE gives you

`RequestContext` — `pid`, `uid`, `gid`, `umask` — is passed to `open`, `lookup`,
`getattr`, `create`, `setattr`, `unlink` and `rename`. It is **not** passed to
`read` or `write`, which get a file handle and nothing else.

So attribution happens at open time or not at all, and the granularity ceiling
is *"process P opened document D at time T with flags F"*. Three consequences,
all measured against the live mount:

- **mmap is invisible.** A `python3` mapping a file logs one open and then
  nothing. There is no read to attribute.
- **The page cache hides repeat readers.** We pass `keep_cache=False`, so every
  open reaches us; with caching left on, a second reader can be served without
  the mount ever hearing about it.
- **Kernel-originated requests have no process.** Readahead and writeback report
  pid 0, and `describe()` returns `None` rather than inventing a caller.

Observed shapes:

    cat[525459]     cat .../engineering/onboarding.md          flags 0x8000
    head[525460]    head -c 10 .../engineering/onboarding.md   flags 0x8000
    grep[525461]    grep -rl Onboarding .../engineering        flags 0x28800  (x3 inodes)
    bash[525452]    bash audit_probe.sh                        flags 0x8201
    python3[525547] python3 -c                                 flags 0x8000

The `grep` line is the interesting one: one pid, three opens, one sweep. That is
the shape an agent walking the tree makes, and it is legible.

`bash` at `0x8201` is `O_WRONLY|O_TRUNC|O_LARGEFILE` — a shell redirect is
attributed to the shell, because `printf` is a builtin. It also confirms the
kernel negotiated `atomic_o_trunc`, which is what the `base_version` guard in
`fs.py` depends on.

## Read it inline, ship it later

The intuition is backwards here. Blocking the open is the *safe* half and the
queue is the risky half.

While we are handling `open`, the caller is blocked in the syscall. It cannot
have exited and its pid cannot have been reused. The same `/proc` read performed
later, off a queue, races both.

And it is free. Measured on this host:

| what | cost |
| --- | --- |
| `/proc/<pid>/exe` (readlink) | 1.6 us |
| `/proc/<pid>/cmdline` | 7.8 us |
| `/proc/<pid>/stat` | 10.0 us |
| **whole bundle** | **32 us** |
| the HTTP content fetch `open()` already blocks on | **~30 ms** |

Three orders of magnitude apart, on localhost. Over a real network the ratio
only gets worse for the HTTP side. There is nothing to defer.

## Which fields mean anything

    {
      "pid": 525937,
      "cmdline": ["sleep", "5"],
      "comm": "sleep",
      "exe": "/nix/store/mp8s10...-coreutils-9.11/bin/coreutils",
      "ppid": 525936,
      "starttime": 123785162,
      "loginuid": 1000
    }

- `cmdline` is argv, which a process may rewrite in place. `comm` is settable
  with `prctl(PR_SET_NAME)`. **Both are hints.**
- `exe` is a kernel-maintained symlink to the actual inode and cannot be pointed
  elsewhere by the process itself. Note above that argv says `sleep` while `exe`
  says `coreutils` — the truthful field is the one nobody typed.
- `loginuid` is stamped by PAM at login and needs `CAP_AUDIT_CONTROL` to change,
  so it survives `su` and `sudo` and says which human's session this descends
  from.
- `starttime` paired with `pid` identifies one process for as long as the box is
  up, which is what makes a queued record survive pid reuse.

**argv leaks credentials, so it is truncated.** `mysql -pSECRET`,
`curl -H "Authorization: ..."` and friends all sit in `/proc/<pid>/cmdline` in
the clear. Shipping full argv means taking secrets that have nothing to do with
the wiki off the user's machine, putting them in someone else's database, and
then in that database's backups — a worse leak than the one an audit trail is
meant to catch.

So `cmdline` is `argv[0]` and nothing else, with the count of what was dropped
alongside it:

    "cmdline": ["bash"], "argv_elided": 3

The count matters. Without it a truncated command line is indistinguishable
from a bare one, and a reader has no way to tell "they ran `bash`" from "they
ran `bash` with three arguments you are not being shown". `exe` is unaffected
and remains the field worth trusting.

`--audit-argv` ships the whole of argv, for a fleet whose policy already says
so. It logs a warning at startup, because that is the sort of flag that gets
set once and then forgotten.

## The trust boundary

The mount runs on the user's own laptop. Every record here is a claim made by
software they control — they can patch the mount, delete the queue, or not run
it. There is no "secret place" to put it: the file is theirs.

That makes this **opt-in telemetry, useful on a managed fleet and worthless
against someone who owns the machine**, which is why `--audit` defaults to off.
The authoritative record of who read what is server-side, against the token, and
that one the client cannot forge.

## Transport

The audit event and the read it describes should land together. Getting there
took a wrong turn worth recording, because the wrong turn is the one that looks
clever.

Two things were measured against PostgREST first:

- Custom request headers *are* visible to Postgres, via
  `current_setting('request.headers')::json` — an `X-Fswiki-Audit` header comes
  through as `{"x-fswiki-audit": "..."}`.
- **A GET cannot write.** PostgREST runs GET in a read-only transaction:
  `cannot execute INSERT in a read-only transaction`. The same function called
  over POST inserts fine.

That second line contains the answer, and it was read as an obstacle for
longer than it should have been.

### The obstacle course

Taking "a GET cannot write" as fixed, there are four ways out of a read-only
transaction, all tested against the running stack:

| route | result |
| --- | --- |
| `SET TRANSACTION READ WRITE` | ✗ `cannot set transaction read-write mode inside a read-only transaction` |
| `set_config('transaction_read_only','off',true)` | ✗ same error — the GUC is not a way round it |
| `pg_notify()` | ✓ **allowed in a read-only transaction** |
| `dblink()` to a second connection | ✓ writes, and the row survives the outer `ROLLBACK` |

The read-only flag applies to *this* transaction's writes. A notification is
not a write, and a second connection is not this transaction. Wire either to
`db-pre-request` — a function PostgREST calls before every request, in the
request's own transaction, where `current_setting('request.headers')` is
readable — and a plain content GET carrying an `X-Fswiki-Audit` header returns
`200` with the document while the audit row lands. Proven end to end, no view
or RLS change involved.

60 requests each against the dev stack, same document:

| route | median | p90 | over baseline |
| --- | --- | --- | --- |
| baseline, no header | 29.9 ms | 34.7 ms | — |
| `pg_notify` | 29.1 ms | 35.3 ms | **free** |
| `dblink`, fresh connection per request | 35.1 ms | 41.9 ms | +5.3 ms |
| `dblink`, connection kept per backend, sync | 32.4 ms | 42.7 ms | +2.5 ms |
| `dblink`, kept, async `send_query` | 29.4 ms | 36.4 ms | **free** |

(Beware a kept-connection guard written as `'x' = any (dblink_get_connections())`:
that function returns NULL rather than `{}` when nothing is open, so the guard
never fires and every request 503s. It benchmarks beautifully, because failing
early is fast.)

Both have a sting. `pg_notify` is **at-most-once** — no listener, no record —
and the notify queue is shared and bounded, so a listener that connects and
then stalls fills it, and once full **commits start failing across the whole
database**. An audit listener that hangs can take the wiki down. `dblink` is
durable, but the connection it opens is a second session driven by an
attacker-supplied header: the probe used `user=postgres`, which puts a
superuser channel one quoting bug away from that header, and doing it properly
means a `postgres_fdw` foreign server, a user mapping and an insert-only role
before it is safe to run at all.

### Not using GET

None of that is necessary. The read-only transaction is a property of the
**verb**, and nothing requires a read to be a GET.

    POST /rpc/read_document   { "p_document": "...", "p_event": { ... } }

`wiki.read_document()` selects the body from `syncable_document` and inserts
the access event in the same transaction. One round trip, both or neither, no
second connection, no header parsing, no privileged anything. The security
question the `dblink` route raised does not come up, because there is no second
session to secure.

It is not even a misuse of the verb, which is the part that feels wrong for a
moment. **A request that records something is not idempotent**, and that is
precisely what POST is for. GET promises that repeating it changes nothing;
an audited read breaks that promise. The audited read was never a GET.

What it costs is HTTP caching — POST responses are not cacheable, and
conditional requests are gone. That is worth nothing here: the mount keeps its
own cache keyed on the document's version, which is stronger than an ETag round
trip, and nothing between the mount and PostgREST was caching anyway. Reads
stay on GET when nobody is auditing, so the cheaper verb is still the default.

The function is `SECURITY INVOKER` over a `security_invoker` view, so it is
exactly as filtered as the GET it replaces. That is the one property worth
testing rather than asserting, and the suite does: it compares the function's
output against the view's, and checks that a document the caller cannot sync
comes back empty through both.

### What this changes about the record

Events off the client queue are the client's word that a read happened. A row
written by `read_document` is the server's own record of having served the
bytes. The `process` field is still the client describing itself, and still
forgeable — but *"this token was handed this document at this time"* becomes
something the server witnessed rather than something it was told.

### Why there is still a queue

The fetch can only carry an event when there is a fetch. There often is not:

* **a content-cache hit** — the mount holds bodies keyed by version, so a
  re-read of an unchanged document never reaches the network;
* **a body served from a draft** — your own unpublished work is local;
* **a refused open** — it fails before any fetch, and an attempt on something
  you may not have is the half worth keeping;
* **a mutation** — a create, a delete or the rename that lands an editor's save
  has no read to ride along on;
* **no network at all** — the laptop is on a train.

So both routes exist and the event is minted once, before either. The client
queues it only if the fetch did not take it, and the id is generated up front
so that when both happen anyway — a retry, a crash between the two — the
server's `on conflict (event_id) do nothing` collapses them into one row. That
is the same idempotency key the queue already needed; it pays for this too.

### What exists

* `wiki.access_event` — insert-only for `fswiki_user`, RLS so you see only your
  own trail. No update or delete grant at all: a trail its subject can rewrite
  is not one. The principal comes from the token, never from the payload.
* `wiki.read_document(uuid, jsonb)` — the POST read above, which records the
  access in the transaction that serves the bytes.
* `wiki.record_opens(jsonb)` — one round trip per queue batch, idempotent on
  the client-generated `event_id`, so a resend returns 0 rather than
  duplicating.
* `fswiki_fuse.audit.AuditLog` — the client queue.

### The queue

1. The event is captured inline and, if no fetch took it, appended as one JSON
   line to `audit.jsonl`, mode 0600, under `--audit-dir`. No fsync per event,
   no network, no lock.
2. A background task ships it, every `--audit-interval` seconds and once more on
   unmount.
3. **Rotation, not truncation.** The live file is renamed to `audit.sending.jsonl`
   before anything is read from it, so events arriving mid-flight land in a fresh
   file and cannot be dropped by a truncate decided before they existed. A batch
   that fails to ship stays under its rotated name and is retried ahead of
   anything newer, which also keeps the order.
4. Delivery is at-least-once. Losing an acknowledgement is common; losing an
   event is not acceptable. `record_opens` dedupes on `event_id`.
5. The queue is size-capped (8 MB). When it drops events it counts them and says
   so in the log, and says so again when the queue drains — a gap nobody can see
   is indistinguishable from nothing having happened.

The cap is seeded from what is already on disk, not from zero. A queue that
survived the last session still occupies the space it was capped by, and
starting the count fresh would let every restart buy another 8 MB — which on a
laptop that mounts twice a day is not a cap at all.

Recorded after the entry resolves and *before* the permission checks, so a
refused operation is recorded too: an attempt on something you may not have is
the more interesting half of an access log. Scratch files never leave the
process and are never recorded.

### What counts as an event

| action | what raised it |
| --- | --- |
| `open` | a file was opened; `open_flags` says whether for writing |
| `create` | a new document, either `create()` or a save onto an empty path |
| `write` | the rename that lands an editor's temp file on a real path |
| `delete` | `unlink` on a document — a draft withdrawn or a page retired |
| `move` | a published document renamed to another path |

Not one row per FUSE operation, because FUSE will not support that: `read()`
and `write()` are handed a file handle and no caller at all, so there is no
process to attribute bytes to. An in-place save — a shell redirect, or vim with
`backupcopy=yes` — carries no rename and appears as an `open` whose flags say
`O_WRONLY|O_TRUNC`. Everything else an editor does goes through the temp-file
rename, which is why that one is `write` rather than `move`: by the time the
draft is posted the caller is long gone, and the rename is the last moment
their identity is still knowable.

### Failure modes worth knowing

* A full or read-only disk logs a warning and drops the event. It never fails
  the `open()`.
* A torn last line — the process was killed mid-write — is skipped, and the rest
  of its batch still ships.
* `--audit` without a token is refused at startup rather than silently
  collecting events that can never be filed: events belong to a principal, and
  anonymous is not one.
