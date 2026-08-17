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

**argv leaks credentials.** `mysql -pSECRET`, `curl -H "Authorization: ..."` and
friends all sit in `/proc/<pid>/cmdline` in the clear. Shipping full argv to a
server means shipping unrelated secrets from the user's machine to the wiki's
database, where they land in backups. Ship `exe` and `argv[0]` by default; put
the full argv behind a separate, louder flag.

## The trust boundary

The mount runs on the user's own laptop. Every record here is a claim made by
software they control — they can patch the mount, delete the queue, or not run
it. There is no "secret place" to put it: the file is theirs.

That makes this **opt-in telemetry, useful on a managed fleet and worthless
against someone who owns the machine**, which is why `--audit` defaults to off.
The authoritative record of who read what is server-side, against the token, and
that one the client cannot forge.

## Transport

Two things were measured against PostgREST, and together they settle the design:

- Custom request headers *are* visible to Postgres, via
  `current_setting('request.headers')::json` — an `X-Fswiki-Audit` header comes
  through as `{"x-fswiki-audit": "..."}`.
- **A GET cannot write.** PostgREST runs GET in a read-only transaction:
  `cannot execute INSERT in a read-only transaction`. The same function called
  over POST inserts fine.

So a GET cannot write **to its own transaction**. That is not the end of it, and
the shortcut turns out to work after all — you just have to leave the
transaction to do it.

### Getting out of the read-only transaction

Four escapes, all tested against the running stack:

| route | result |
| --- | --- |
| `SET TRANSACTION READ WRITE` | ✗ `cannot set transaction read-write mode inside a read-only transaction` |
| `set_config('transaction_read_only','off',true)` | ✗ same error — the GUC is not a way round it |
| `pg_notify()` | ✓ **allowed in a read-only transaction** |
| `dblink()` to a second connection | ✓ writes, and the row survives the outer `ROLLBACK` |

The read-only flag applies to *this* transaction's writes. A notification is not
a write, and a second connection is not this transaction.

### The hook

`db-pre-request` is a function PostgREST calls before every request, in the
request's own transaction, and it can read `current_setting('request.headers')`.
That is the whole mechanism — no view or RLS change is involved.

Proven end to end: a plain content GET carrying
`X-Fswiki-Audit: {"comm":"vim","pid":525459,"exe":"...","loginuid":1000}`
returned `200` with the document, and the audit row landed. On the request, no
extra round trip.

### What it costs

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

### Which one, and what it costs you

**`pg_notify` is free but it is at-most-once.** No listener, no record; a
listener restart is a silent gap. Worse, the notify queue is shared and bounded:
a listener that connects and then stalls fills it, and once full **commits start
failing across the whole database**. An audit listener that hangs can take the
wiki down. Same conclusion the notification bridge reached in
[change-notification.md](change-notification.md) — good as a signal, not as the
record.

**`dblink` with a kept connection is the durable one**, at +2.5 ms synchronous.
Prefer sync over async: the async variant is free only because it never looks at
the result, so failures surface one request late and the last write of a session
never reports at all.

Two things to get right before this goes anywhere near production:

- **Do not connect as a superuser.** The probe used `user=postgres`, which gives
  every request a superuser channel out of a `SECURITY DEFINER` function whose
  input is an attacker-supplied header. `format(%L)` quoting is correct and is
  also the only thing standing between that header and SQL injection into a
  superuser session. Use a dedicated role with `INSERT` on one table and nothing
  else, so the worst case is a junk audit row.
- **Put the credentials in the catalog, not the function body.** A
  `postgres_fdw` foreign server plus a user mapping keeps the connection string
  out of a routine anyone can read with `\df+`.

### Still worth a client-side queue

The header covers opens that reach the network. It does not cover a content
cache hit, and it does not cover being offline — so the durable local queue
below is still the at-least-once path, with the header as the zero-round-trip
fast path for everything online. The design that follows:

1. `open()` captures inline and appends to an in-memory buffer. Never blocks on
   disk or network.
2. A writer task appends JSONL to `$FSWIKI_STATE/audit.jsonl`, mode 0600. No
   fsync per event; batch it.
3. A shipper task batch-POSTs to an RPC and advances an offset on success.
4. Every record carries a client-generated event id, and the server table is
   insert-only with `unique (event_id)` and `on conflict do nothing`, so
   at-least-once delivery is safe.
5. The on-disk queue is size-capped. When it drops records it writes a marker
   saying how many, so a gap is visible rather than silent.
