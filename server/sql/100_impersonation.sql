-- Impersonation: answering "what does this person actually see" by being them,
-- rather than by reconstructing it from the ACL.
--
-- Read docs/impersonation.md first. The three load-bearing decisions:
--
--   1. It enters at wiki.current_user_id() and nowhere else, so the mount, the
--      CLI, the preview server and the renderer all inherit it with no change
--      and none of them can forget.
--   2. It is read-only by `set transaction read only`, not by a checklist of
--      write paths. document_version.author_id is permanent published history
--      and an impersonated push would forge into it irrecoverably.
--   3. It refuses to run in a transaction that cannot record it. An
--      impersonation nobody can audit is the abuse the feature invites.

------------------------------------------------------------------------------
-- Who may act as whom
------------------------------------------------------------------------------
--
-- Not a capability in the document lattice. The lattice answers questions about
-- a *path*; this is about an *identity*, and capabilities_at('root.x', bob) has
-- no slot for it and should not grow one.

create table wiki.impersonation_grant (
  id          uuid primary key default gen_random_uuid(),
  -- Expanded through effective_principals, so a grant may name `wiki-admins`
  -- rather than one row per admin.
  actor_id    uuid not null references wiki.principal(id) on delete cascade,
  -- Also expanded, so a grant naming `everyone` covers every *person*. Note it
  -- does not thereby cover every *group*: groups here belong to no groups, so
  -- acting as a membership needs the groups named. That asymmetry is real and
  -- deliberate -- see may_impersonate_groups below.
  subject_id  uuid not null references wiki.principal(id) on delete cascade,
  note        text,
  expires_at  timestamptz,
  created_at  timestamptz not null default now(),
  created_by  uuid references wiki.principal(id) on delete set null,

  constraint impersonation_no_self check (actor_id <> subject_id),
  constraint impersonation_grant_key unique (actor_id, subject_id)
);

comment on table wiki.impersonation_grant is
  'Actor may act as subject. Both sides expand through effective_principals. '
  'A limited grant is the ordinary case; unlimited is the special one -- a '
  'grant whose subject is `everyone`.';

------------------------------------------------------------------------------
-- The log
------------------------------------------------------------------------------
--
-- Written by the same statement that authorises the impersonation, so it cannot
-- be skipped by a caller and there is no window in which one happened without
-- the other.

create table wiki.impersonation_event (
  id           uuid primary key default gen_random_uuid(),
  -- The human. Never the subject: an audit trail that can be written as someone
  -- else is worse than none, because it is trusted.
  actor_id     uuid not null references wiki.principal(id) on delete cascade,
  -- Exactly one of these two.
  subject_id   uuid references wiki.principal(id) on delete cascade,
  subject_groups uuid[],
  occurred_at  timestamptz not null default now(),
  method       text,
  path         text,

  constraint impersonation_event_one_subject
    check ((subject_id is null) <> (subject_groups is null))
);

create index impersonation_event_actor_idx
  on wiki.impersonation_event (actor_id, occurred_at desc);

comment on table wiki.impersonation_event is
  'One row per impersonated request, written before the transaction is locked '
  'read-only. The actor is the token holder, always.';

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
    insert into wiki.impersonation_event (actor_id, subject_id, method, path)
      values (v_actor, p_subject, p_method, p_path);
    perform set_config('fswiki.act_as', p_subject::text, true);
  else
    if not wiki.may_impersonate_groups(v_actor, p_groups) then
      raise exception 'not permitted to act as that membership'
        using errcode = 'insufficient_privilege';
    end if;
    insert into wiki.impersonation_event (actor_id, subject_groups, method, path)
      values (v_actor, p_groups, p_method, p_path);
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

alter table wiki.impersonation_grant enable row level security;
alter table wiki.impersonation_event enable row level security;

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
