

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

  -- A session, not a request. The hook runs per request and a mount makes a
  -- great many of them -- measured, a single `ls` of an impersonated mount is
  -- four -- so a row each would bury the thing anyone actually wants to know
  -- under its own volume. "dave acted as bob for forty minutes, 1,200
  -- requests" is both smaller and a better answer to the question the table
  -- exists for than 1,200 rows saying the same thing.
  --
  -- The fidelity given up is per-request timing, which belongs to an access
  -- log; this is not one. What it must never lose is that the impersonation
  -- happened at all, and collapsing repeats cannot lose that.
  occurred_at  timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  requests     integer not null default 1,
  -- Of the request that opened the session.
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

alter table wiki.impersonation_grant enable row level security;

alter table wiki.impersonation_event enable row level security;
