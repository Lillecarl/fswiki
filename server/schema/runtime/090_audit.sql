-- 090_audit.sql, the runtime half.
-- The file header, and the reasoning, are in ../tables/090_audit.sql.

-- You may see your own trail, and nothing else. Reading everyone's is an
-- administrative act and belongs to a role that does not serve web requests.
-- authenticated_user_id(), not current_user_id(): impersonation reproduces
-- someone's view of the *wiki*, and their access log is not part of it. An
-- impersonated session reads the actor's own trail, which is also the trail the
-- impersonation is being written into.
create policy access_event_select_own on wiki.access_event
  for select using (principal_id = wiki.authenticated_user_id());

-- Insert-only, and only about yourself. There is deliberately no update or
-- delete policy: an audit trail its subject can edit is not one. (They can of
-- course decline to send anything at all — see the trust note above.)
create policy access_event_insert_own on wiki.access_event
  for insert with check (principal_id = wiki.authenticated_user_id());

grant select, insert on wiki.access_event to fswiki_user;

------------------------------------------------------------------------------
-- Recording
------------------------------------------------------------------------------

-- Take a batch of events. One round trip for a queue flush, and the caller's
-- identity comes from the token rather than the payload, so a client cannot
-- file events against somebody else.
--
-- Returns how many were new. A client that resends a batch gets 0 and can drop
-- it, which is the whole point of the client-generated event_id.
create or replace function wiki.record_opens(p_events jsonb)
returns integer
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  v_user  uuid := wiki.authenticated_user_id();
  v_acted uuid := nullif(wiki.current_user_id(), wiki.authenticated_user_id());
  v_added integer;
begin
  if v_user is null then
    raise exception 'recording access events requires an authenticated caller'
      using errcode = 'insufficient_privilege';
  end if;
  if jsonb_typeof(p_events) <> 'array' then
    raise exception 'expected an array of events'
      using errcode = 'invalid_parameter_value';
  end if;

  insert into wiki.access_event
    (event_id, principal_id, acted_as, document_id, path, occurred_at, action,
     open_flags, process)
  select (e ->> 'event_id')::uuid,
         v_user,
         v_acted,
         (e ->> 'document_id')::uuid,
         (e ->> 'path')::ltree,
         (e ->> 'occurred_at')::timestamptz,
         coalesce(e ->> 'action', 'open'),
         (e ->> 'open_flags')::integer,
         e -> 'process'
    from jsonb_array_elements(p_events) e
  -- Resends are expected, not exceptional.
  on conflict (event_id) do nothing;

  get diagnostics v_added = row_count;
  return v_added;
end;
$$;

comment on function wiki.record_opens(jsonb) is
  'Record a batch of client-reported opens. Idempotent on event_id, so the '
  'client queue can retry freely. The principal comes from the token, never '
  'from the payload.';

grant execute on function wiki.record_opens(jsonb) to fswiki_user;

------------------------------------------------------------------------------
-- Reading a document, and recording that you did
------------------------------------------------------------------------------

-- The same content a GET on `syncable_document` returns, plus the access
-- event, in one round trip and one transaction.
--
-- The verb is the whole trick. PostgREST runs GET in a **read-only**
-- transaction, which is why recording an access on the read itself looked
-- like it needed an escape hatch — pg_notify, or dblink to a second
-- connection, both of which mean a privileged channel driven by client input
-- (docs/audit-trail.md has the measurements). POST has no such restriction.
-- And it is not a misuse of the verb: a request that records something is not
-- idempotent, which is precisely what POST is for. The read is a side effect
-- of the recording as much as the other way round.
--
-- SECURITY INVOKER, and `syncable_document` is itself a security_invoker view,
-- so this is exactly as filtered as the GET it replaces. It is a second way to
-- read what you could already read, not a second answer about what you may.
--
-- What it changes is who is making the claim. Events off the client queue are
-- the client's word that a read happened; this row is the server's own record
-- of having served the bytes. The `process` field is still the client
-- describing itself and still forgeable — but "this token was handed this
-- document at this time" becomes something the server witnessed.
create or replace function wiki.read_document(
  p_document uuid,
  p_event    jsonb default null
)
returns table (content text)
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  v_user    uuid := wiki.authenticated_user_id();
  v_acted   uuid := nullif(wiki.current_user_id(), wiki.authenticated_user_id());
  v_content text;
  v_found   boolean;
begin
  select d.content into v_content
    from wiki.syncable_document d
   where d.id = p_document;
  v_found := found;

  -- Recorded whether or not the read succeeded: a request for something you
  -- cannot have is the more interesting half of an access log. document_id is
  -- only filled in when the row was actually visible, because the foreign key
  -- would otherwise reject a probe for an id that does not exist and take the
  -- read down with it. The path travels in the payload regardless, so a probe
  -- still leaves a mark.
  -- Under impersonation the transaction is already read only, so there is no
  -- access event to be had here -- the hook wrote an impersonation_event before
  -- locking it, which is the record that matters for an impersonated read. The
  -- read still has to work, so this is a skip rather than a failure.
  if v_user is not null and p_event is not null
     and current_setting('transaction_read_only') = 'off' then
    insert into wiki.access_event
      (event_id, principal_id, acted_as, document_id, path, occurred_at, action,
       open_flags, process)
    values (
      (p_event ->> 'event_id')::uuid,
      v_user,
      v_acted,
      case when v_found then p_document end,
      (p_event ->> 'path')::ltree,
      coalesce((p_event ->> 'occurred_at')::timestamptz, now()),
      coalesce(p_event ->> 'action', 'open'),
      (p_event ->> 'open_flags')::integer,
      p_event -> 'process'
    )
    -- The client queues the same event under the same id before it gets here,
    -- so whichever arrives second is a no-op. That is what lets the queue be a
    -- fallback for the cases this cannot see — a refused open, a body served
    -- from a draft, a laptop with no network — without double-counting the
    -- ones it can.
    on conflict (event_id) do nothing;
  end if;

  if v_found then
    return query select v_content;
  end if;
  return;
end;
$$;

grant execute on function wiki.read_document(uuid, jsonb) to fswiki_user;

-- The same read, for the other kind of client.
--
-- wiki.read_document() above goes through syncable_document, and that is not
-- an implementation detail -- it is the `sync` capability being enforced by the
-- server rather than by the client's good manners. A document that is readable
-- but not syncable comes back as no rows, which is exactly what a FUSE mount
-- or the CLI should get.
--
-- A browser is the other case, and it is the case `sync` was invented for.
-- Denying sync is documented as leaving a page "perfectly readable in the
-- browser while keeping it off laptops -- every view then costs a request the
-- server can log". A renderer that read through syncable_document could not
-- serve those pages at all, so the audit lever would take away the very thing
-- it exists to produce.
--
-- Hence two functions rather than one with a flag. Which view a caller reads
-- through is a permission decision, and a permission decision that arrives as
-- an argument is one the caller gets to make. These are two grants instead,
-- and today both are held by everyone -- but they are separable the day a
-- deployment wants a renderer that cannot mirror, or a mirror that cannot
-- render.
create or replace function wiki.view_document(
  p_document uuid,
  p_event    jsonb default null
)
returns table (content text)
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  v_user    uuid;
  v_acted   uuid;
  v_content text;
  v_found   boolean;
begin
  select d.content into v_content
    from wiki.current_document d
   where d.id = p_document;
  v_found := found;

  -- Identical to read_document's, deliberately: the same trail, the same
  -- at-least-once event_id, the same rule that a refused read is the more
  -- interesting half of an access log. Only the view above differs.
  --
  -- The identity lookup is guarded by the presence of a token rather than by
  -- its result, which is not a micro-optimisation. fswiki_anon holds EXECUTE
  -- on neither authenticated_user_id() nor current_user_id() -- it has no
  -- business resolving identity -- so reaching them at all would make every
  -- anonymous page view fail with "permission denied for function". Asking
  -- whether a request carries a token needs no grant: it is a GUC read.
  --
  -- The consequence is that an unauthenticated read writes no audit row, which
  -- is the same answer read_document gives and for the same reason:
  -- access_event names the human who caused the read, and for a public page
  -- there isn't one. See 070_public_test.sql.
  if p_event is not null
     and current_setting('transaction_read_only') = 'off'
     and coalesce(current_setting('request.jwt.claims', true), '') <> '' then
    v_user  := wiki.authenticated_user_id();
    v_acted := nullif(wiki.current_user_id(), v_user);
  end if;

  if v_user is not null then
    insert into wiki.access_event
      (event_id, principal_id, acted_as, document_id, path, occurred_at, action,
       open_flags, process)
    values (
      (p_event ->> 'event_id')::uuid,
      v_user,
      v_acted,
      case when v_found then p_document end,
      (p_event ->> 'path')::ltree,
      coalesce((p_event ->> 'occurred_at')::timestamptz, now()),
      coalesce(p_event ->> 'action', 'open'),
      (p_event ->> 'open_flags')::integer,
      p_event -> 'process'
    )
    on conflict (event_id) do nothing;
  end if;

  if v_found then
    return query select v_content;
  end if;
  return;
end;
$$;

comment on function wiki.view_document(uuid, jsonb) is
  'Read one document''s body for display and record the access in the same '
  'transaction. Gated on `read`, through current_document: this is the browser '
  'read, and it serves pages that read_document deliberately will not.';

-- Restated on the older function, because the two being told apart by name
-- alone is what let docs/rendering.md point the renderer at the wrong one.
comment on function wiki.read_document(uuid, jsonb) is
  'Read one document''s body for mirroring and record the access in the same '
  'transaction. Gated on `sync`, through syncable_document: a readable but '
  'un-syncable document returns no rows. POST rather than GET because '
  'PostgREST runs GET read-only, and a GET cannot write its own audit row.';

grant execute on function wiki.view_document(uuid, jsonb) to fswiki_user;

-- And to anonymous callers, so the server serves a public page and a private
-- one by the same code path. No principal argument, nothing to probe with:
-- what comes back is what current_document shows fswiki_anon, which is what
-- was granted to `public`.
grant execute on function wiki.view_document(uuid, jsonb) to fswiki_anon;
