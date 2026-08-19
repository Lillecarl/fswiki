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

insert into wiki.change_counter default values;

comment on table wiki.change_counter is
  'One row, one number, bumped by wiki.note_change(). Read through '
  'wiki.change_token(); the value is opaque and only equality is meaningful.';

-- SECURITY DEFINER because it runs as whoever did the write, and fswiki_user
-- has no business writing this table directly -- there is no grant for it
-- below, and there should not be.
create or replace function wiki.note_change()
returns trigger
language plpgsql volatile security definer
set search_path = wiki, public, pg_temp as $$
begin
  update wiki.change_counter set changes = changes + 1;
  return null;
end;
$$;

-- Statement-level, not row-level: a push that publishes forty documents is one
-- change to notice, not forty, and the token carries no information about what
-- moved anyway.
--
-- The trigger fires even when the statement matched no rows, so an UPDATE with
-- a WHERE that selects nothing still bumps the counter where the WAL position
-- would not have moved. That is one more false positive in a token whose
-- contract already permits them, and the alternative -- transition tables on
-- every write in the system -- costs more than the refetch it saves.
--
-- What is deliberately **not** here is the audit trail: `access_event` and
-- `impersonation_event`. Recording that somebody read something does not
-- change what anybody can read, and treating it as a change is precisely the
-- bug this file exists to fix. `impersonation_grant` is out for the same
-- reason from the other end: revoking a grant stops an actor impersonating,
-- but it does not alter any manifest, and a client that refetched would get
-- the same answer or a 403 either way.

create trigger principal_change after insert or update or delete
  on wiki.principal for each statement execute function wiki.note_change();

create trigger user_account_change after insert or update or delete
  on wiki.user_account for each statement execute function wiki.note_change();

create trigger group_member_change after insert or update or delete
  on wiki.group_member for each statement execute function wiki.note_change();

create trigger role_change after insert or update or delete
  on wiki.role for each statement execute function wiki.note_change();

create trigger role_capability_change after insert or update or delete
  on wiki.role_capability for each statement execute function wiki.note_change();

create trigger role_inherits_change after insert or update or delete
  on wiki.role_inherits for each statement execute function wiki.note_change();

create trigger capability_requires_change after insert or update or delete
  on wiki.capability_requires for each statement execute function wiki.note_change();

create trigger document_change after insert or update or delete
  on wiki.document for each statement execute function wiki.note_change();

create trigger document_version_change after insert or update or delete
  on wiki.document_version for each statement execute function wiki.note_change();

create trigger draft_change after insert or update or delete
  on wiki.draft for each statement execute function wiki.note_change();

create trigger ace_change after insert or update or delete
  on wiki.ace for each statement execute function wiki.note_change();

-------------------------------------------------------------------------------
-- The token
-------------------------------------------------------------------------------

create or replace function wiki.change_token()
returns text
language sql stable parallel safe
set search_path = wiki, public, pg_temp as $$
  select changes::text from wiki.change_counter;
$$;

-- The counter is bumped inside the writing transaction, so the new value
-- becomes visible at the moment the change it describes does. Two samples
-- taken inside one transaction are always equal — correct, and occasionally
-- surprising.

comment on function wiki.change_token() is
  'Opaque token that changes whenever anything a client can see is written. '
  'Poll it and refetch only when it moves. Conservative: it advances for '
  'writes that did not affect your view, so clients may refresh needlessly, '
  'but it can never miss a change. Treat the value as opaque — only equality '
  'is meaningful.';

-- Deliberately not SECURITY DEFINER and deliberately global.
--
-- Global is sound even though visibility is per-user: if nothing changed for
-- anyone, nothing changed for you. The converse — a token that moves when your
-- own view did not — costs one wasted manifest fetch and leaks nothing, because
-- the value carries no information about *what* changed.
--
-- Not SECURITY DEFINER because it does not need to be: a bare SELECT grant on
-- one opaque number discloses nothing, and a function that reads a table the
-- caller may read is a function nobody has to audit.

grant select on wiki.change_counter to fswiki_user;
grant execute on function wiki.change_token() to fswiki_user;
