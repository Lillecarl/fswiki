# Telling clients something changed

A FUSE mount has to notice other people's edits. This is what PostgREST offers
for that, what it costs, and what we should do.

All numbers below are measured against the `fswiki-dev` stack — PostgreSQL 18.4,
PostgREST 14.14 — with the dev fixtures loaded.

## What PostgREST gives you: nothing

There is no WebSocket, no SSE, no long-poll. It is a stateless request/response
API and that is the whole design.

It is worth being precise about the two things people assume are there:

**No cache validators.** A response carries `Content-Range` and
`Content-Location` and nothing else — no `ETag`, no `Last-Modified`, no
`Cache-Control`. So there is no conditional GET, and a poll cannot come back as
a cheap `304`. Every poll transfers the whole body.

**No aggregates by default.** `?select=updated_at.max()` returns

    {"code":"PGRST123","message":"Use of aggregate functions is not allowed"}

They can be turned on with `db-aggregates-enabled`, which is off precisely
because an unbounded aggregate over a large table is a denial-of-service lever.
We should leave it off.

PostgREST *does* use `LISTEN/NOTIFY` — you can see it in the dev log, connected
and listening on the `pgrst` channel — but only for its own schema-cache and
config reloads. Nothing about your data reaches a client that way.

## What we should do: poll a change token

One tiny RPC returning a value that moves whenever anything changes. The client
polls that and only re-fetches the manifest when it differs.

| | payload | 100 sequential requests |
| --- | --- | --- |
| change token | **11 B** | 884 ms |
| full manifest | 6053 B | 22 855 ms |

(Measured against the first implementation below, whose token was a WAL
position. The one that replaced it is a decimal counter and slightly smaller;
the round-trip cost is the same, and it is the round trip that dominates.)

**26x faster and 550x less data.** That is what makes a 1-second poll interval
reasonable where a 5-second manifest refresh was not.

### The first answer, and why it was wrong

`pg_current_wal_lsn()` works as the token with no schema change at all —
`fswiki_user` can already execute it, and it advances on any write to the
cluster. It is *conservative*: it moves for writes that have nothing to do with
the wiki, so clients occasionally refresh for nothing, but it can never miss a
change. That is the right direction to be wrong in.

    create or replace function wiki.change_token()
    returns text language sql stable parallel safe as
    $$ select pg_current_wal_lsn()::text $$;

That is what shipped, and it failed for exactly the client that needed a cheap
poll most. **The impersonation hook writes on every impersonated request.** It
collapses a session into one row rather than logging each request separately,
but collapsing still means an `UPDATE` of `last_seen_at` and `requests`, in the
same transaction that serves the read. So the WAL advanced on every poll, and
the token an impersonated client got back was different every time.

Measured — five polls, nothing else touching the database:

| | distinct tokens in five polls |
| --- | --- |
| ordinary client | **1** |
| impersonated client | **5** |

An impersonated mount therefore refetched the whole manifest on every tick,
which is precisely what `wiki.changed()` was added to prevent, and paid an
extra round trip per tick to do it. Nothing looked broken: the token is opaque,
and a client that refetches too often still shows the right thing.

### What it is now

A counter, bumped by statement-level triggers on the tables that hold what a
client can see — `document`, `document_version`, `draft`, `ace`, `principal`,
`user_account`, `group_member`, and the role tables. Same signature, so no
client changed.

Two things about it are load-bearing.

**It is a table, not a sequence.** A sequence's value is visible the moment
`nextval` runs, *before* the writing transaction commits. A client polling in
that window would see a new token, refetch the old data, and then never refetch
again because the token had stopped moving. That is a missed change, and a
missed change is the one thing this must never do. A row updated inside the
transaction becomes visible when the transaction does — the same property the
WAL position had, and the reason the WAL position was a reasonable first
answer.

**The audit trail is not what paid for it.** The cheap fix would have been to
make the hook write less often — debounce `last_seen_at`, approximate the
request count — and that buys a poll optimisation with the record of who acted
as whom. Excluding `access_event` and `impersonation_event` from the triggers
costs nothing instead: writing down that somebody read something does not
change what anybody can read.

The price is that every write transaction takes a row lock on one row and so
serialises against every other write transaction. Reads never touch it, and the
writes are pushes and draft saves.

**A global token is sound even though visibility is per-user.** If nothing
changed for anyone, nothing changed for you. The reverse — a token that moved
when your view did not — costs one wasted manifest fetch.

## If polling is not enough

`postgres-websockets` (by PostgREST's author, packaged in nixpkgs at 0.12.0.0)
bridges `LISTEN/NOTIFY` to authenticated WebSockets and shares PostgREST's JWT
secret, so tokens minted by `fswiki-token` work unchanged. A sidecar of
`psycopg` + `LISTEN` fanning out to SSE is about fifty lines if we would rather
own it.

**Whatever we use, the payload must not contain content, paths, or document
ids.** A bridge cannot evaluate row-level security per subscriber, so anything
it broadcasts is readable by everyone connected — which would hand out exactly
what the ACL exists to withhold. Emit the new token and nothing else, and let
each client re-fetch through PostgREST where RLS applies. This keeps the
notification tiny as a side effect.

**Do not long-poll through PostgREST.** Every waiting request pins a connection
from the pool for its whole duration, and the pool is small (4 in dev). A dozen
idle clients would lock out real traffic.

## Where this leaves the FUSE client

It currently re-fetches the manifest every `--ttl` seconds, default 5, and tells
the kernel it may cache entries for the same period. That is a full 6 KB fetch
per tick per client whether or not anything happened.

The change should be: poll `change_token()` on a short interval, refresh the
manifest only when the token moves. Own writes already force a refresh, so this
only affects how fast someone else's edit shows up.

Later, subscribing instead of polling is a drop-in replacement for the polling
loop — the refresh path does not change, only what triggers it.
