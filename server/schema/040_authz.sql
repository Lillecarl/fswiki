-- Authorization: who am I, which principals do I count as, what may I do here.
--
-- SECURITY NOTE
-- -------------
-- Identity is read from the `request.jwt.claims` GUC, which is the convention
-- PostgREST sets after verifying the token. Any role that can execute arbitrary
-- SQL can also SET that GUC, so this is only safe for clients that *cannot*
-- issue arbitrary statements — i.e. everything arriving over PostgREST. A
-- client connecting with libpq directly must instead be authenticated as its
-- own database role; see wiki.current_user_id() for where to hook that in.
--
-- The helpers below are SECURITY DEFINER because they are called from RLS
-- policies and must read the ACL tables regardless of the caller's own
-- visibility. Keep `ace` and friends free of FORCE ROW LEVEL SECURITY so the
-- owning role continues to bypass RLS inside these functions.

create or replace function wiki.jwt_claims()
returns jsonb
language sql stable parallel safe as $$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  );
$$;

create or replace function wiki.authenticated_user_id()
returns uuid
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select ua.principal_id
    from wiki.user_account ua
   where ua.is_active
     and ua.oidc_issuer  = wiki.jwt_claims() ->> 'iss'
     and ua.oidc_subject = wiki.jwt_claims() ->> 'sub';
$$;

comment on function wiki.authenticated_user_id() is
  'Principal id of the token holder, or NULL when unauthenticated. Never '
  'impersonated. If you add direct libpq clients, resolve them here from '
  'current_user rather than the JWT GUC.';

-- IMPERSONATION
-- -------------
-- Two GUCs, both transaction-local, both set only by wiki.begin_impersonation()
-- in 100_impersonation.sql after it has checked a grant and written the log.
-- Nothing else in the schema knows they exist: the whole feature enters through
-- current_user_id() and leaves through effective_principals().
--
--   fswiki.act_as         a real principal's uuid -- "show me bob's wiki"
--   fswiki.act_as_groups  a set of group uuids    -- "show me a regular
--                                                    engineer's wiki"
--
-- The second is not the first with a group in it. See docs/impersonation.md:
-- naming a group as the subject under-reports (a group is in no other groups,
-- and nobody is in only one group) and over-reports (a deny naming `everyone`
-- never reaches a group). The set is the unit because membership is.

create or replace function wiki.act_as_groups()
returns uuid[]
language sql stable parallel safe as $$
  select nullif(current_setting('fswiki.act_as_groups', true), '')::uuid[];
$$;

-- A principal id for a membership that belongs to nobody.
--
-- Deliberately derived from the group set rather than random, so that "acted as
-- {everyone, engineering}" is the same subject in every request and the
-- impersonation log can be grouped by it.
--
-- The version nibble is forced to '0', which gen_random_uuid() -- a v4
-- generator, so always '4' -- cannot produce. That is what makes "matches no
-- row in wiki.principal" a property of the value rather than a probability,
-- and everything downstream depends on it: is_superuser() finds no row,
-- document.owner_id never matches, draft.author_id never matches. A
-- hypothetical office worker owns nothing and has no drafts, which is correct
-- and costs no code.
create or replace function wiki.synthetic_principal_id(p_groups uuid[])
returns uuid
language sql immutable parallel safe as $$
  select overlay(
           md5('fswiki:act_as_groups:' ||
               (select coalesce(string_agg(g::text, ',' order by g), '')
                  from unnest(p_groups) g))
           placing '0' from 13 for 1)::uuid;
$$;

-- The effective principal: what every policy, view and helper resolves against.
--
-- One chokepoint on purpose. The mount, the CLI, the preview server and the
-- renderer all ask this one question, so impersonation needs no client change
-- and there is nothing for one of them to forget.
create or replace function wiki.current_user_id()
returns uuid
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select coalesce(
    nullif(current_setting('fswiki.act_as', true), '')::uuid,
    case when cardinality(coalesce(wiki.act_as_groups(), '{}'::uuid[])) > 0
         then wiki.synthetic_principal_id(wiki.act_as_groups()) end,
    wiki.authenticated_user_id());
$$;

comment on function wiki.current_user_id() is
  'Principal id the request is acting as. Equals authenticated_user_id() unless '
  'the request is impersonating. Use authenticated_user_id() for anything that '
  'must name the human: the audit trail, and the impersonation check itself.';

create or replace function wiki.is_superuser(p_user uuid default wiki.current_user_id())
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select coalesce(
    (select ua.is_superuser from wiki.user_account ua
      where ua.principal_id = p_user and ua.is_active),
    false);
$$;

-- The caller, plus every group they belong to, transitively. This is the set of
-- principals an ACE may name to match them.
--
-- The second seed row is the whole of group impersonation. A synthetic
-- principal has no rows in group_member -- it has no rows anywhere -- so its
-- memberships come from the GUC instead, and the recursion then expands them
-- upward by the ordinary rule. Nested groups, deny ACEs and role inheritance
-- all behave exactly as they do for a person, because from here down nothing
-- can tell the difference.
create or replace function wiki.effective_principals(p_user uuid default wiki.current_user_id())
returns table (principal_id uuid)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  with recursive expanded as (
    select p_user as id
     where p_user is not null
    union
    select g as id
      from unnest(coalesce(wiki.act_as_groups(), '{}'::uuid[])) g
     where p_user = wiki.synthetic_principal_id(wiki.act_as_groups())
    union
    select gm.group_id
      from wiki.group_member gm
      join expanded e on gm.member_id = e.id
  )
  select id from expanded;
$$;

-- Every capability a role confers, following inheritance upward.
create or replace function wiki.role_capabilities(p_role uuid)
returns table (capability wiki.capability)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  with recursive expanded as (
    select p_role as id
    union
    select ri.inherits_role_id
      from wiki.role_inherits ri
      join expanded e on ri.role_id = e.id
  )
  select distinct rc.capability
    from wiki.role_capability rc
    join expanded e on rc.role_id = e.id;
$$;

------------------------------------------------------------------------------
-- Capability implication
------------------------------------------------------------------------------

-- Everything p_cap requires, transitively, plus itself. Allowing a capability
-- allows all of these: you cannot write a document you may not read.
create or replace function wiki.capability_downward(p_cap wiki.capability)
returns table (capability wiki.capability)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  with recursive walk as (
    select p_cap as cap
    union
    select cr.requires
      from wiki.capability_requires cr
      join walk w on cr.capability = w.cap
  )
  select cap from walk;
$$;

-- Everything that requires p_cap, transitively, plus itself. Denying a
-- capability denies all of these: taking read away takes write with it.
create or replace function wiki.capability_upward(p_cap wiki.capability)
returns table (capability wiki.capability)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  with recursive walk as (
    select p_cap as cap
    union
    select cr.capability
      from wiki.capability_requires cr
      join walk w on cr.requires = w.cap
  )
  select cap from walk;
$$;

-- Does an ACE carrying this role speak to this capability at all? The closure
-- direction depends on whether the ACE grants or removes access — see the
-- comment on wiki.capability_requires.
create or replace function wiki.ace_covers(
  p_role uuid,
  p_cap  wiki.capability,
  p_type wiki.ace_type
)
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select exists (
    select 1
      from wiki.role_capabilities(p_role) rc
     where case p_type
             when 'allow' then p_cap in (select c.capability
                                           from wiki.capability_downward(rc.capability) c)
             else              p_cap in (select c.capability
                                           from wiki.capability_upward(rc.capability) c)
           end
  );
$$;

------------------------------------------------------------------------------
-- The ACL walk
------------------------------------------------------------------------------
--
-- WHY THE CORE IS KEYED ON PATH, NOT ON DOCUMENT ID
--
-- An RLS policy that resolves permissions by looking the row up by id cannot
-- decide `INSERT ... RETURNING`. RETURNING applies the SELECT policy to the row
-- just inserted, but these helpers are STABLE and therefore read the snapshot
-- as of statement start, where that row does not yet exist — so the lookup
-- finds nothing, the ACL comes back empty, and the insert is refused. PostgREST
-- adds RETURNING to every insert by default, so this is not a corner case.
--
-- The same trap catches UPDATE differently and more quietly: a WITH CHECK that
-- re-reads the row by id sees the *old* path, so re-parenting a document into a
-- subtree the caller has no rights over would sail through the check.
--
-- So the real implementations take the values that make up a document's
-- identity for ACL purposes — path, is_folder, owner_id — and the policies on
-- wiki.document pass their own columns. The id-keyed wrappers below are for
-- everything else, where the row certainly exists.

-- Ancestors of a path, nearest first, stopping where inheritance is blocked.
--
-- Distance 0 is the document at that path, 1 its parent, and so on. A document
-- with inheritance_blocked still contributes its own ACEs; what it stops is
-- everything *above* it, for itself and its whole subtree. A path with no row
-- yet simply has no distance-0 entry, which is correct: a document that does
-- not exist has no ACEs of its own, only what it will inherit.
create or replace function wiki.acl_chain(p_path ltree)
returns table (document_id uuid, distance integer)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  -- Enumerate the ancestors and match them by equality, rather than asking GiST
  -- for `d.path @> p_path`. Same answer, and on a 110k-document tree it is
  -- ~4.5x faster when called per row the way RLS calls it (measured: 50ms vs
  -- 230ms over 5000 rows). A GiST probe descends an index; this is a handful of
  -- btree lookups against document_path_key, which are cached and depth-bounded.
  with chain as (
    select d.id, d.inheritance_blocked,
           nlevel(p_path) - nlevel(d.path) as distance
      from wiki.document d
     where d.path = any (array(select subpath(p_path, 0, n)
                                 from generate_series(1, nlevel(p_path)) n))
  ),
  cutoff as (
    select min(distance) as depth from chain where inheritance_blocked
  )
  select c.id, c.distance
    from chain c
   where c.distance <= coalesce((select depth from cutoff), c.distance)
   order by c.distance;
$$;

create or replace function wiki.acl_chain(p_document uuid)
returns table (document_id uuid, distance integer)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select * from wiki.acl_chain((select path from wiki.document where id = p_document));
$$;

-- Resolve one capability against the effective ACL, returning 'allow', 'deny',
-- or NULL when no ACE mentions it at all.
--
-- Precedence is the canonical NTFS order: nearest ancestor first, and deny
-- before allow at equal distance. So an explicit deny on the document beats an
-- explicit allow on it, which beats an inherited deny from the parent, which
-- beats an inherited allow from the parent, and so on up the tree.
--
-- Note this differs from the "deny always wins globally" rule: here a deny
-- inherited from a folder CAN be overridden by an explicit allow placed on one
-- document inside it. That is the whole point of per-object ACLs, and it is why
-- an admin CLI must show which ACE won, not just the verdict.
create or replace function wiki.resolve_ace(
  p_path      ltree,
  p_is_folder boolean,
  p_cap       wiki.capability,
  p_user      uuid default wiki.current_user_id()
)
returns wiki.ace_type
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select a.ace_type
    from wiki.acl_chain(p_path) c
    join wiki.ace a on a.document_id = c.document_id
   where a.principal_id in (select ep.principal_id
                              from wiki.effective_principals(p_user) ep)
     and (a.expires_at is null or a.expires_at > now())
     and case
           when c.distance = 0 then not a.inherit_only
           else (case when p_is_folder then a.container_inherit
                                       else a.object_inherit end)
                and (not a.no_propagate or c.distance = 1)
         end
     and wiki.ace_covers(a.role_id, p_cap, a.ace_type)
   order by c.distance, (a.ace_type = 'deny') desc
   limit 1;
$$;

-- The single question every policy asks. Absent any matching ACE the answer is
-- no: the ACL is a closed world.
--
-- This is the form the wiki.document policies use, because they can supply the
-- new row's own columns and so stay correct for INSERT/UPDATE ... RETURNING.
create or replace function wiki.can(
  p_path      ltree,
  p_is_folder boolean,
  p_owner     uuid,
  p_cap       wiki.capability,
  p_user      uuid default wiki.current_user_id()
)
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select case
    when p_user is null or p_path is null then false
    when wiki.is_superuser(p_user) then true
    -- The owner's standing right to repair the ACL, and nothing more.
    when p_cap = 'grant'
     and p_owner in (select ep.principal_id from wiki.effective_principals(p_user) ep)
      then true
    else coalesce(wiki.resolve_ace(p_path, p_is_folder, p_cap, p_user) = 'allow', false)
  end;
$$;

-- Convenience form for everything that is not a policy on wiki.document itself:
-- ACEs, revisions, drafts, the CLI. Safe wherever the row is known to exist.
create or replace function wiki.has_capability(
  p_document uuid,
  p_cap      wiki.capability,
  p_user     uuid default wiki.current_user_id()
)
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select coalesce(
    (select wiki.can(d.path, d.is_folder, d.owner_id, p_cap, p_user)
       from wiki.document d where d.id = p_document),
    false);
$$;

-- Traversal.
--
-- Read access to a document is useless if the folders above it are invisible:
-- the FUSE mount would show a readable page at a path nobody can `cd` into, and
-- `ls /` would come back empty. A folder is therefore also visible when it
-- contains something the caller may read, at any depth.
--
-- This intentionally leaks folder names along the route to a readable document.
-- That is inherent to exposing a hierarchy at all; if a folder's name is the
-- secret, nothing beneath it may be granted out. (Windows makes the same trade
-- with its Traverse Folder right and the Bypass Traverse Checking privilege.)
--
-- Parameterised by capability so the same rule serves both consumers: the web
-- renderer needs folders on the route to a *readable* document, the FUSE client
-- needs folders on the route to a *syncable* one, and those are different trees
-- for anyone holding a deny-sync ACE.
--
-- Cost note: this evaluates the full ACL of each descendant rather than
-- guessing from ACE placement, so it is O(subtree) per folder. Correct first.
-- If it shows up in a profile, materialise a per-user visible-path set per
-- request.
create or replace function wiki.can_traverse(
  p_path ltree,
  p_cap  wiki.capability default 'read',
  p_user uuid default wiki.current_user_id()
)
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select case
    when p_user is null then false
    when wiki.is_superuser(p_user) then true
    else exists (
      select 1
        from wiki.document child
       where child.path <@ p_path
         and child.path <> p_path
         and wiki.can(child.path, child.is_folder, child.owner_id, p_cap, p_user)
    )
  end;
$$;

create or replace function wiki.can_traverse(
  p_document uuid,
  p_cap      wiki.capability default 'read',
  p_user     uuid default wiki.current_user_id()
)
returns boolean
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  select wiki.can_traverse(
    (select path from wiki.document where id = p_document), p_cap, p_user);
$$;

-- Everything the caller may do at a document. This is what the CLI reads back
-- and what the FUSE driver exposes as an xattr, so `getfattr` on a mounted file
-- answers "why can't I write this?" without a trip through the docs.
create or replace function wiki.capabilities_at(
  p_document uuid,
  p_user     uuid default wiki.current_user_id()
)
returns wiki.capability[]
language sql stable parallel safe as $$
  select coalesce(array_agg(c order by c), '{}')
    from unnest(enum_range(null::wiki.capability)) c
   where wiki.has_capability(p_document, c, p_user);
$$;

-- Why did that come out the way it did? Returns the effective ACL as the admin
-- CLI should print it: every applicable ACE, in the order they are consulted,
-- with the winner for each capability marked. With deny no longer globally
-- absolute, "explain" stops being a nicety and becomes the only way to debug a
-- permission complaint.
create or replace function wiki.explain_acl(
  p_document uuid,
  p_user     uuid default wiki.current_user_id()
)
returns table (
  capability   wiki.capability,
  verdict      text,
  distance     integer,
  source_path  ltree,
  ace_type     wiki.ace_type,
  principal    text,
  role         text,
  inherited    boolean
)
language sql stable security definer parallel safe
set search_path = wiki, public, pg_temp as $$
  -- wiki.can() decides two capabilities without consulting the ACL at all, and
  -- both have to be modelled here or this function contradicts the one it
  -- exists to explain. That is worse than not having it: a permission complaint
  -- gets debugged against an answer the enforcement path never gave.
  --
  -- The owner's standing 'grant' is the one that used to be missing. A document
  -- owner who holds no ACE at all still comes back from capabilities_at() as
  -- {grant}, while this said `grant: deny (no matching ACE)` — correct about
  -- the ACL, wrong about the outcome.
  with subject as (
    select d.path,
           wiki.is_superuser(p_user) as by_superuser,
           exists (select 1 from wiki.effective_principals(p_user) ep
                    where ep.principal_id = d.owner_id) as by_owner,
           owner.name as owner_name
      from wiki.document d
      left join wiki.principal owner on owner.id = d.owner_id
     where d.id = p_document
  )
  select
    caps.c,
    case
      when o.superuser then 'allow (superuser)'
      when o.owner     then 'allow (owner)'
      when winner.ace_type = 'allow' then 'allow'
      when winner.ace_type = 'deny'  then 'deny'
      else 'deny (no matching ACE)'
    end,
    -- An override is not a distance-N ACE, so the columns that describe *which*
    -- ACE won say so rather than pointing at one that lost. The owner rule does
    -- attach to a document, so that much is worth reporting.
    case when o.superuser or o.owner then 0    else winner.distance end,
    case when o.superuser then null
         when o.owner     then s.path
         else src.path end,
    case when o.superuser or o.owner then null else winner.ace_type end,
    case when o.superuser then null
         when o.owner     then s.owner_name
         else prin.name end,
    case when o.superuser or o.owner then null else r.name end,
    case when o.superuser or o.owner then false else winner.distance > 0 end
  from subject s
  cross join unnest(enum_range(null::wiki.capability)) as caps(c)
  cross join lateral (
    select s.by_superuser as superuser,
           -- Ownership buys the right to repair the ACL, and nothing else.
           s.by_owner and caps.c = 'grant' as owner
  ) o
  left join lateral (
    select a.ace_type, c.distance, a.document_id, a.principal_id, a.role_id
      from wiki.acl_chain(p_document) c
      join wiki.ace a on a.document_id = c.document_id
      cross join lateral (select is_folder from wiki.document where id = p_document) target
     where a.principal_id in (select ep.principal_id from wiki.effective_principals(p_user) ep)
       and (a.expires_at is null or a.expires_at > now())
       and case
             when c.distance = 0 then not a.inherit_only
             else (case when target.is_folder then a.container_inherit
                                              else a.object_inherit end)
                  and (not a.no_propagate or c.distance = 1)
           end
       and wiki.ace_covers(a.role_id, caps.c, a.ace_type)
     order by c.distance, (a.ace_type = 'deny') desc
     limit 1
  ) winner on true
  left join wiki.document  src  on src.id  = winner.document_id
  left join wiki.principal prin on prin.id = winner.principal_id
  left join wiki.role      r    on r.id    = winner.role_id
  order by caps.c;
$$;
