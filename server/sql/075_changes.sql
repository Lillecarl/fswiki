-- "Has anything changed?", in eleven bytes.
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

create or replace function wiki.change_token()
returns text
language sql stable parallel safe as $$
  select pg_current_wal_lsn()::text;
$$;

-- The WAL position advances on commit, not while a transaction runs, so the
-- token becomes visible at exactly the moment the change it describes does.
-- Two samples taken inside one transaction are always equal — correct, and
-- occasionally surprising.

comment on function wiki.change_token() is
  'Opaque token that changes whenever the database is written. Poll it and '
  'refetch only when it moves. Conservative: it advances for writes unrelated '
  'to the wiki, so clients may refresh needlessly, but it can never miss a '
  'change. Treat the value as opaque — only equality is meaningful.';

-- Deliberately not SECURITY DEFINER and deliberately global.
--
-- Global is sound even though visibility is per-user: if nothing changed for
-- anyone, nothing changed for you. The converse — a token that moves when your
-- own view did not — costs one wasted manifest fetch and leaks nothing, because
-- the value carries no information about *what* changed.
--
-- If the false positives ever become expensive, replace the body with a counter
-- bumped by statement-level triggers on document, document_version, ace,
-- group_member and user_account. The signature stays, so no client changes.

grant execute on function wiki.change_token() to fswiki_user;
