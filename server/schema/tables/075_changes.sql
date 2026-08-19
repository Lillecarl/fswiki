

-- "Has anything changed?", in a handful of bytes.
--
-- PostgREST offers no push and no cache validators: no WebSocket, no SSE, no
-- ETag, so a client that wants to notice someone else's edit has to ask. The
-- whole point of this function is to make asking cheap enough to do often.
--
--   change token   11 B     884 ms / 100 requests
--   full manifest  6053 B  22855 ms / 100 requests
--
-- See docs/change-notification.md for the measurements and for why a
-- notification bridge must never carry content.

-------------------------------------------------------------------------------
-- The counter
-------------------------------------------------------------------------------
--
-- This was `pg_current_wal_lsn()`, and the WAL position is the obvious answer:
-- free, monotonic, and it advances for every commit without anyone having to
-- remember to bump it. It had one fault, and the fault was fatal to the only
-- client that most needed a cheap poll.
--
-- The impersonation hook writes an `impersonation_event` row on **every**
-- impersonated request — collapsed into one session row, but an UPDATE of
-- `last_seen_at` and `requests` all the same, in the same transaction that
-- serves the read. So the WAL moved on every poll, and the token an
-- impersonated client got back was different every single time. Measured:
-- five polls with nothing else touching the database gave one token for an
-- ordinary client and five distinct tokens for an impersonated one.
--
-- The effect was exactly what `wiki.changed()` was added to prevent — an
-- impersonated mount refetching the whole manifest on every poll — with an
-- extra round trip per poll on top, and it was invisible because the token is
-- opaque and a client that refetches too often still shows the right thing.
--
-- So: a counter, bumped by statement-level triggers on the tables that hold
-- what a client can see. Which is what the comment at the bottom of this file
-- used to propose for a different reason, and the shape it proposed is right.
--
-- It is a **table and not a sequence**, and that is the whole design. A
-- sequence's value is visible the moment `nextval` runs, before the writing
-- transaction commits — so a client could poll in that window, see a new
-- token, refetch the *old* data, and then never refetch again because the
-- token has stopped moving. That is a missed change, and a missed change is
-- the one thing this must never do. A row updated inside the transaction
-- becomes visible when the transaction does, which is the same property the
-- WAL position had.
--
-- The cost is that every write transaction takes a row lock on this one row
-- and therefore serialises against every other write transaction. For a wiki
-- that is a price worth paying: reads never touch it, and the writes are
-- pushes and draft saves.

create table wiki.change_counter (
  -- One row, enforced. `only_row` is the primary key and is checked true, so a
  -- second row cannot be inserted and the update below needs no WHERE clause.
  only_row boolean primary key default true check (only_row),
  changes  bigint not null default 0
);

comment on table wiki.change_counter is
  'One row, one number, bumped by wiki.note_change(). Read through '
  'wiki.change_token(); the value is opaque and only equality is meaningful.';
