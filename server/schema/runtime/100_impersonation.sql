-- 100_impersonation.sql, the runtime half.
-- The file header, and the reasoning, are in ../tables/100_impersonation.sql.

-- How long a gap ends a session. Long enough that ordinary use is one row,
-- short enough that coming back tomorrow is a new one.
create or replace function wiki.impersonation_session_gap()
returns interval language sql immutable parallel safe as $$
  select interval '5 minutes';
$$;

------------------------------------------------------------------------------
-- The checks
------------------------------------------------------------------------------

-- May p_actor act as the person p_subject?
--
-- The superuser guard is not about tidy grant tables: without it one row naming
-- a superuser turns a diagnostic into full privilege escalation.
--
-- Transitive impersonation is impossible by construction rather than by a
-- check, because every caller of this passes authenticated_user_id(), which
-- impersonation never changes.
create or replace function wiki.may_impersonate(p_actor uuid, p_subject uuid)
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select p_actor is not null
     and p_subject is not null
     and p_actor <> p_subject
     and (wiki.is_superuser(p_actor) or not wiki.is_superuser(p_subject))
     and exists (
       select 1
         from wiki.impersonation_grant g
        where (g.expires_at is null or g.expires_at > now())
          and g.actor_id   in (select principal_id from wiki.effective_principals(p_actor))
          and g.subject_id in (select principal_id from wiki.effective_principals(p_subject)));
$$;

-- May p_actor act as a membership of p_groups?
--
-- Every group must be granted individually. It is tempting to skip the check
-- for groups the actor already belongs to -- a subset of your own memberships
-- ought to give a subset of your own view -- but deny ACEs break that: dropping
-- a group can drop a *deny*. The fixtures contain exactly that case.
--
-- The subject side is not expanded here. Expansion answers "is this principal
-- covered by that grant", which for a person means their groups; a group has no
-- groups, so expanding would only ever match the grant naming the group itself.
create or replace function wiki.may_impersonate_groups(p_actor uuid, p_groups uuid[])
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select p_actor is not null
     and p_groups is not null
     and cardinality(p_groups) > 0
     and not exists (
       -- every named principal must be a group, and must be granted
       select 1
         from unnest(p_groups) g
        where not exists (
                select 1 from wiki.principal p
                 where p.id = g and p.kind = 'group')
           or not exists (
                select 1
                  from wiki.impersonation_grant ig
                 where ig.subject_id = g
                   and (ig.expires_at is null or ig.expires_at > now())
                   and ig.actor_id in
                       (select principal_id from wiki.effective_principals(p_actor))));
$$;

------------------------------------------------------------------------------
-- Entering it
------------------------------------------------------------------------------

-- Extend the open session, or open one. Called only from begin_impersonation,
-- and only while the transaction can still be written to.
create or replace function wiki.note_impersonation(
  p_actor uuid, p_subject uuid, p_groups uuid[], p_method text, p_path text
) returns void
language plpgsql volatile security definer
set search_path = wiki, public, pg_temp as $$
begin
  with open_session as (
    select e.id
      from wiki.impersonation_event e
     where e.actor_id = p_actor
       and e.subject_id     is not distinct from p_subject
       and e.subject_groups is not distinct from p_groups
       and e.last_seen_at > now() - wiki.impersonation_session_gap()
     order by e.last_seen_at desc
     limit 1
  )
  update wiki.impersonation_event e
     set last_seen_at = now(),
         requests     = e.requests + 1
    from open_session s
   where e.id = s.id;

  if not found then
    insert into wiki.impersonation_event
      (actor_id, subject_id, subject_groups, method, path)
    values (p_actor, p_subject, p_groups, p_method, p_path);
  end if;
end;
$$;

-- Authorise, record, switch, lock. In that order, in one statement.
--
-- SECURITY DEFINER because it runs as the caller's role, which has no business
-- writing impersonation_event -- and must not, since it is the record of what
-- that caller did.
create or replace function wiki.begin_impersonation(
  p_subject uuid    default null,
  p_groups  uuid[]  default null,
  p_method  text    default null,
  p_path    text    default null
) returns void
language plpgsql volatile security definer
set search_path = wiki, public, pg_temp as $$
declare
  v_actor uuid := wiki.authenticated_user_id();
begin
  if p_subject is null and (p_groups is null or cardinality(p_groups) = 0) then
    return;
  end if;

  if p_subject is not null and p_groups is not null then
    raise exception 'impersonate a person or a membership, not both'
      using errcode = 'invalid_parameter_value';
  end if;

  if v_actor is null then
    raise exception 'impersonation requires an authenticated caller'
      using errcode = 'insufficient_privilege';
  end if;

  -- The refusal that shapes the feature. PostgREST decides the transaction's
  -- mode before this hook runs, so the insert below would otherwise fail with a
  -- bare 25006 and no explanation.
  --
  -- Test the capability, not the verb. "GET is read-only" is the visible half;
  -- the actual rule is the function's *volatility* -- measured, POST
  -- /rpc/change_token also runs read-only, because change_token is `stable`.
  -- A check for the verb would have been wrong in a way nobody would notice
  -- until an impersonated request quietly failed on an endpoint that looked
  -- like it should work.
  if current_setting('transaction_read_only') = 'on' then
    raise exception 'impersonation cannot be recorded in a read-only transaction'
      using errcode = 'insufficient_privilege',
            hint = 'Use the POST endpoints; a GET cannot write its own log. '
                   'See docs/impersonation.md.';
  end if;

  if p_subject is not null then
    -- One message for "no grant" and for "that person is a superuser". Telling
    -- them apart would answer a question the caller was not entitled to ask.
    if not wiki.may_impersonate(v_actor, p_subject) then
      raise exception 'not permitted to act as %',
        coalesce((select name from wiki.principal where id = p_subject), p_subject::text)
        using errcode = 'insufficient_privilege';
    end if;
    perform wiki.note_impersonation(v_actor, p_subject, null, p_method, p_path);
    perform set_config('fswiki.act_as', p_subject::text, true);
  else
    if not wiki.may_impersonate_groups(v_actor, p_groups) then
      raise exception 'not permitted to act as that membership'
        using errcode = 'insufficient_privilege';
    end if;
    perform wiki.note_impersonation(v_actor, null, p_groups, p_method, p_path);
    perform set_config('fswiki.act_as_groups', p_groups::text, true);
  end if;

  -- Last, and the reason there is no list of write paths to keep up to date.
  -- set_config rather than SET TRANSACTION READ ONLY because this runs inside a
  -- function and after a write; transaction_read_only is the flag that command
  -- sets, and setting it directly is legal in both positions. `true` scopes it
  -- to the transaction, so the pooled connection goes back writable.
  perform set_config('transaction_read_only', 'on', true);
end;
$$;

-- Resolve a principal by uuid or by name, for the header form.
create or replace function wiki.principal_ref(p_ref text, p_kind wiki.principal_kind default null)
returns uuid
language plpgsql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
declare
  v_ref  text := trim(p_ref);
  v_uuid uuid;
  v_id   uuid;
begin
  if p_ref is null or length(v_ref) = 0 then
    return null;
  end if;
  begin
    v_uuid := v_ref::uuid;
  exception when invalid_text_representation then
    v_uuid := null;
  end;
  select p.id into v_id
    from wiki.principal p
   where ((v_uuid is not null and p.id = v_uuid)
       or (v_uuid is null and p.name = v_ref))
     and (p_kind is null or p.kind = p_kind);
  if v_id is null then
    raise exception 'no such principal: %', p_ref
      using errcode = 'invalid_parameter_value';
  end if;
  return v_id;
end;
$$;

------------------------------------------------------------------------------
-- The PostgREST hook
------------------------------------------------------------------------------
--
-- Wire up with:
--
--   alter role fswiki_authenticator set pgrst.db_pre_request = 'wiki.pre_request';
--   notify pgrst, 'reload config';
--
-- Two headers, and it is an error to send both:
--
--   Fswiki-Act-As:        bob            -- a person, by name or uuid
--   Fswiki-Act-As-Groups: everyone,engineering
--
-- Names are accepted because this is a maintainer's tool and uuids are not
-- something anyone types. Resolution raises on an unknown name rather than
-- silently acting as nobody, which would look like the feature working.
create or replace function wiki.pre_request()
returns void
language plpgsql volatile security definer
set search_path = wiki, public, pg_temp as $$
declare
  v_headers jsonb := coalesce(
    nullif(current_setting('request.headers', true), '')::jsonb, '{}'::jsonb);
  v_person  text  := v_headers ->> 'fswiki-act-as';
  v_groups  text  := v_headers ->> 'fswiki-act-as-groups';
  v_ids     uuid[];
begin
  if coalesce(v_person, '') = '' and coalesce(v_groups, '') = '' then
    return;
  end if;

  if coalesce(v_groups, '') <> '' then
    select array_agg(wiki.principal_ref(g, 'group'))
      into v_ids
      from unnest(string_to_array(v_groups, ',')) g
     where length(trim(g)) > 0;
  end if;

  perform wiki.begin_impersonation(
    case when coalesce(v_person, '') = '' then null
         else wiki.principal_ref(v_person, 'user') end,
    v_ids,
    nullif(current_setting('request.method', true), ''),
    nullif(current_setting('request.path', true), ''));
end;
$$;

------------------------------------------------------------------------------
-- Visibility
------------------------------------------------------------------------------
--
-- Both tables are readable by the people they name and by nobody else. Neither
-- is writable over the API at all: grants are administered out of band, and a
-- log its subject can edit is not a log.

-- Deliberately argument-free. is_superuser(uuid) is not granted to fswiki_user
-- and should not be: an RLS policy is evaluated with the querying role's
-- privileges, so a policy calling it would hand every client a way to ask "is
-- this uuid a superuser" about anyone. This asks only about the caller.
create or replace function wiki.caller_is_superuser()
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select wiki.is_superuser(wiki.authenticated_user_id());
$$;

create policy impersonation_grant_select on wiki.impersonation_grant
  for select using (
    wiki.caller_is_superuser()
    or actor_id in (select principal_id
                      from wiki.effective_principals(wiki.authenticated_user_id())));

-- Deliberately keyed on authenticated_user_id(), not current_user_id(): an
-- impersonated session must not be able to read the subject's own record of
-- being impersonated, and an actor must never lose sight of their own.
create policy impersonation_event_select on wiki.impersonation_event
  for select using (
    wiki.caller_is_superuser()
    or actor_id = wiki.authenticated_user_id());

grant select on wiki.impersonation_grant, wiki.impersonation_event to fswiki_user;

-- authenticated_user_id() and act_as_groups() are granted in 060_roles.sql,
-- beside current_user_id() and for the same reason: RLS policies call them.
grant execute on function
    wiki.caller_is_superuser(),
    wiki.synthetic_principal_id(uuid[]),
    wiki.may_impersonate(uuid, uuid),
    wiki.may_impersonate_groups(uuid, uuid[]),
    wiki.principal_ref(text, wiki.principal_kind)
  to fswiki_user;

-- The hook runs as the authenticator, before any SET ROLE.
grant execute on function wiki.pre_request() to fswiki_authenticator, fswiki_user, fswiki_anon;

------------------------------------------------------------------------------
-- Reads that can be impersonated
------------------------------------------------------------------------------
--
-- The hook refuses any transaction it cannot log in, and PostgREST gives a GET
-- a read-only transaction. So every GET-shaped read in the client is closed to
-- impersonation, which is most of them: the manifest, the draft list and a
-- document by path.
--
-- These are those three reads as `volatile` RPCs, which is what makes PostgREST
-- open a read-write transaction. They are SECURITY INVOKER over the same views
-- the GETs use, so they are the *same reads*, not a second opinion about what a
-- caller may see -- exactly the relationship read_document has to
-- `GET /syncable_document`, and for a related reason.
--
-- `volatile` is not a fib told to get a transaction mode. Under impersonation
-- the result genuinely depends on a GUC set part-way through the transaction,
-- which is what volatile means. Marking them stable would be the inaccurate
-- choice.
--
-- Returning `setof` a view keeps PostgREST's `?select=` working, so the client
-- asks for the same columns over either path and nothing downstream branches.

create or replace function wiki.list_documents()
returns setof wiki.syncable_document
language sql volatile
set search_path = wiki, public, pg_temp as $$
  select * from wiki.syncable_document order by path;
$$;

create or replace function wiki.document_at(p_path ltree)
returns setof wiki.syncable_document
language sql volatile
set search_path = wiki, public, pg_temp as $$
  select * from wiki.syncable_document where path = p_path;
$$;

create or replace function wiki.list_drafts()
returns setof wiki.draft
language sql volatile
set search_path = wiki, public, pg_temp as $$
  select * from wiki.draft;
$$;

comment on function wiki.list_documents() is
  'The manifest, as an RPC. Identical rows to GET /syncable_document; exists '
  'because a GET runs in a read-only transaction and impersonation must be '
  'able to log itself.';

grant execute on function
    wiki.list_documents(),
    wiki.document_at(ltree),
    wiki.list_drafts()
  to fswiki_user;

-- change_token(), for the same reason -- and this one is not a nicety. Without
-- it an impersonated client cannot ask "has anything changed?", so a mount
-- refetches the whole manifest every poll, which is both a pointless six
-- kilobytes and a steady drip into the log above.
create or replace function wiki.changed()
returns text
language sql volatile
set search_path = wiki, public, pg_temp as $$
  select wiki.change_token();
$$;

grant execute on function wiki.changed() to fswiki_user;

-- whoami, for the same reason. current_user_id() is `stable`, so PostgREST runs
-- it read-only even over POST and impersonation refuses it -- which would mean
-- a client could impersonate but never confirm that it had. The name is the
-- honest one: under impersonation this is not who you are.
create or replace function wiki.acting_as()
returns uuid
language sql volatile
set search_path = wiki, public, pg_temp as $$
  select wiki.current_user_id();
$$;

grant execute on function wiki.acting_as() to fswiki_user;
