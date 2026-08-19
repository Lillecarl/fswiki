-- wiki.acl_context / wiki.can_ctx: the ACL, derived once per statement.
--
-- This is a second implementation of the rule wiki.can() implements, written
-- because the first one costs 445 us per document and a wiki is read by the
-- treeful. A second implementation of a security rule is a thing to be
-- nervous about: it is wrong in a way that is invisible until somebody reads
-- something they should not, or fails to read something they should.
--
-- So it is not spot-checked. wiki.can() stays as the specification, and this
-- file compares the two over the full cross product -- every document, every
-- capability, every user, both values of is_folder -- the same shape of proof
-- that holds wiki.ace_closure to wiki.ace_covers_uncached() in 080.
--
-- The fixtures alone would not be a proof of much: they exercise inheritance
-- and a couple of denies, and nothing else. So this file builds a subtree
-- first, one branch per ACE flag, and asserts at the end that every branch
-- actually changed an answer. An equivalence test over a tree where every
-- answer is the same is a test that passes because it asked nothing.

------------------------------------------------------------------------------
-- 0. A tree with one branch per flag
------------------------------------------------------------------------------

create or replace function wiki_test.mkdoc(p_path text, p_folder boolean)
returns uuid
language sql security definer
set search_path = wiki, public, pg_temp as $$
  insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
  select d.id,
         subpath(p_path::ltree, nlevel(p_path::ltree) - 1, 1)::text,
         p_folder, p_path, wiki.principal_id('user', 'alice')
    from wiki.document d
   where d.path = subpath(p_path::ltree, 0, nlevel(p_path::ltree) - 1)
  returning id;
$$;

-- One ACE, by path and role name, with the four flags spelled out.
create or replace function wiki_test.mkace(
  p_path text, p_principal uuid, p_role text, p_type wiki.ace_type,
  p_ci boolean default true, p_oi boolean default true,
  p_io boolean default false, p_np boolean default false,
  p_expires timestamptz default null)
returns void
language sql security definer
set search_path = wiki, public, pg_temp as $$
  insert into wiki.ace (document_id, principal_id, role_id, ace_type,
                        container_inherit, object_inherit, inherit_only,
                        no_propagate, expires_at)
  select d.id, p_principal, wiki.role_id(p_role), p_type,
         p_ci, p_oi, p_io, p_np, p_expires
    from wiki.document d where d.path = p_path::ltree;
$$;

select wiki_test.mkdoc('root.ctx', true);

-- object_inherit off: the folders below inherit, the pages do not.
select wiki_test.mkdoc('root.ctx.ci', true);
select wiki_test.mkdoc('root.ctx.ci.page', false);
select wiki_test.mkdoc('root.ctx.ci.sub', true);
select wiki_test.mkdoc('root.ctx.ci.sub.page', false);
select wiki_test.mkace('root.ctx.ci', wiki_test.who('carol'), 'reader', 'allow',
                       p_ci => true, p_oi => false);

-- container_inherit off: the pages inherit, the folders do not.
select wiki_test.mkdoc('root.ctx.oi', true);
select wiki_test.mkdoc('root.ctx.oi.page', false);
select wiki_test.mkdoc('root.ctx.oi.sub', true);
select wiki_test.mkdoc('root.ctx.oi.sub.page', false);
select wiki_test.mkace('root.ctx.oi', wiki_test.who('carol'), 'reader', 'allow',
                       p_ci => false, p_oi => true);

-- inherit_only: everything below, but not the folder the ACE sits on.
select wiki_test.mkdoc('root.ctx.io', true);
select wiki_test.mkdoc('root.ctx.io.page', false);
select wiki_test.mkace('root.ctx.io', wiki_test.who('carol'), 'reader', 'allow',
                       p_io => true);

-- no_propagate: the immediate children and no further.
select wiki_test.mkdoc('root.ctx.np', true);
select wiki_test.mkdoc('root.ctx.np.page', false);
select wiki_test.mkdoc('root.ctx.np.sub', true);
select wiki_test.mkdoc('root.ctx.np.sub.page', false);
select wiki_test.mkace('root.ctx.np', wiki_test.who('carol'), 'reader', 'allow',
                       p_np => true);

-- A deny inside an allow, and a nearer allow inside that deny. This is the
-- precedence rule that makes deny non-absolute, and it is the one a set-based
-- rewrite is most likely to get wrong.
select wiki_test.mkdoc('root.ctx.deny', true);
select wiki_test.mkdoc('root.ctx.deny.sub', true);
select wiki_test.mkdoc('root.ctx.deny.sub.page', false);
select wiki_test.mkdoc('root.ctx.deny.sub.allowed', false);
select wiki_test.mkace('root.ctx.deny', wiki_test.who('carol'), 'editor', 'allow');
select wiki_test.mkace('root.ctx.deny.sub', wiki_test.who('carol'), 'reader', 'deny');
select wiki_test.mkace('root.ctx.deny.sub.allowed', wiki_test.who('carol'),
                       'reader', 'allow');

-- Deny and allow on the same document: deny wins at equal distance.
select wiki_test.mkdoc('root.ctx.tie', false);
select wiki_test.mkace('root.ctx.tie', wiki_test.who('carol'), 'reader', 'allow');
select wiki_test.mkace('root.ctx.tie', wiki_test.who('carol'), 'reader', 'deny');

-- inheritance_blocked, both halves of it. The block stops what is *above*
-- the document it sits on; the document still contributes its own ACEs, and
-- those still reach downward. Getting only the first half right is the easy
-- mistake, so both are built and both are asserted.
--
-- Under `cut` the grant is on the ancestor, so the block takes it away:
select wiki_test.mkdoc('root.ctx.cut', true);
select wiki_test.mkdoc('root.ctx.cut.open', false);
select wiki_test.mkdoc('root.ctx.cut.blocked', true);
select wiki_test.mkdoc('root.ctx.cut.blocked.page', false);
select wiki_test.mkdoc('root.ctx.cut.blocked.own', false);
select wiki_test.mkace('root.ctx.cut', wiki_test.who('carol'), 'reader', 'allow');
select wiki_test.mkace('root.ctx.cut.blocked.own', wiki_test.who('carol'),
                       'reader', 'allow');
update wiki.document set inheritance_blocked = true
 where path = 'root.ctx.cut.blocked'::ltree;

-- Under `blocked` the grant is on the blocked folder itself, so it survives
-- and still reaches the page below it:
select wiki_test.mkdoc('root.ctx.blocked', true);
select wiki_test.mkdoc('root.ctx.blocked.page', false);
select wiki_test.mkace('root.ctx.blocked', wiki_test.who('carol'), 'reader', 'allow');
update wiki.document set inheritance_blocked = true
 where path = 'root.ctx.blocked'::ltree;

-- An expired ACE grants nothing, and must not be in the context either.
select wiki_test.mkdoc('root.ctx.expired', true);
select wiki_test.mkdoc('root.ctx.expired.page', false);
select wiki_test.mkace('root.ctx.expired', wiki_test.who('carol'), 'reader', 'allow',
                       p_expires => now() - interval '1 day');

-- Through a group rather than a person, and through `public`, which is the
-- one every caller is in including the ones with no account.
select wiki_test.mkdoc('root.ctx.grp', true);
select wiki_test.mkdoc('root.ctx.grp.page', false);
select wiki_test.mkace('root.ctx.grp', wiki_test.grp('everyone'), 'reader', 'allow');
select wiki_test.mkdoc('root.ctx.pub', true);
select wiki_test.mkdoc('root.ctx.pub.page', false);
select wiki_test.mkdoc('root.ctx.pub.hidden', false);
select wiki_test.mkace('root.ctx.pub', wiki_test.grp('public'), 'reader', 'allow');
select wiki_test.mkace('root.ctx.pub.hidden', wiki_test.grp('public'), 'reader', 'deny');

------------------------------------------------------------------------------
-- 1. The two implementations agree, over everything
------------------------------------------------------------------------------

-- Every document x every capability x every user, plus the caller with no
-- account at all -- and both values of is_folder for each, because the policy
-- passes the row's own and a rewrite could quietly stop reading it.
create or replace view wiki_test.ctx_cross as
  select d.path, d.owner_id, f.is_folder, c.capability, u.id as principal
    from wiki.document d
    cross join (values (true), (false)) as f(is_folder)
    cross join (select unnest(enum_range(null::wiki.capability)) as capability) c
    cross join (select p.id from wiki.principal p where p.kind = 'user'
                 union all select null::uuid) u;

select wiki_test.expect_eq('can_ctx agrees with can, over every combination',
  (select count(*)::int from wiki_test.ctx_cross x
    where wiki.can(x.path, x.is_folder, x.owner_id, x.capability, x.principal)
      is distinct from
          wiki.can_ctx(x.path, x.is_folder, x.owner_id, x.capability,
                       wiki.acl_context(x.capability, x.principal))), 0);

-- And over paths that hold no document. wiki.can() answers for those too --
-- it is what document_insert asks about a row that does not exist yet -- and
-- the answer comes from the ancestors, which is exactly what a context holds.
select wiki_test.expect_eq('can_ctx agrees on paths with no document',
  (select count(*)::int
     from wiki.document d
     cross join (values ('absent'), ('also-absent')) as n(slug)
     cross join (values (true), (false)) as f(is_folder)
     cross join (select unnest(enum_range(null::wiki.capability)) as capability) c
     cross join (select p.id from wiki.principal p where p.kind = 'user'
                  union all select null::uuid) u
    where wiki.can(d.path || n.slug::ltree, f.is_folder, null, c.capability, u.id)
      is distinct from
          wiki.can_ctx(d.path || n.slug::ltree, f.is_folder, null, c.capability,
                       wiki.acl_context(c.capability, u.id))), 0);

-- Neither all-true nor all-false: an equivalence between two functions that
-- both always say no is not evidence of anything.
select wiki_test.expect('the cross product contains both answers',
  (select count(*) from wiki_test.ctx_cross x
    where wiki.can_ctx(x.path, x.is_folder, x.owner_id, x.capability,
                       wiki.acl_context(x.capability, x.principal))) > 0
  and (select count(*) from wiki_test.ctx_cross x
        where not wiki.can_ctx(x.path, x.is_folder, x.owner_id, x.capability,
                               wiki.acl_context(x.capability, x.principal))) > 0);

------------------------------------------------------------------------------
-- 2. Every branch of the tree above actually changed an answer
------------------------------------------------------------------------------
--
-- Without these, section 1 could be passing because none of the flags did
-- anything. Each of these is carol's verdict on `read`, and each is the flag's
-- whole purpose.

create or replace function wiki_test.carol_reads(p_path text, p_folder boolean)
returns boolean
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select wiki.can(p_path::ltree, p_folder, null, 'read', wiki.principal_id('user', 'carol'));
$$;

select wiki_test.expect_eq('object_inherit off: the sub-folder yes, the page no',
  array[wiki_test.carol_reads('root.ctx.ci.sub', true),
        wiki_test.carol_reads('root.ctx.ci.page', false)],
  array[true, false]);

select wiki_test.expect_eq('container_inherit off: the page yes, the sub-folder no',
  array[wiki_test.carol_reads('root.ctx.oi.page', false),
        wiki_test.carol_reads('root.ctx.oi.sub', true)],
  array[true, false]);

select wiki_test.expect_eq('inherit_only: below yes, the folder itself no',
  array[wiki_test.carol_reads('root.ctx.io.page', false),
        wiki_test.carol_reads('root.ctx.io', true)],
  array[true, false]);

select wiki_test.expect_eq('no_propagate: the child yes, the grandchild no',
  array[wiki_test.carol_reads('root.ctx.np.page', false),
        wiki_test.carol_reads('root.ctx.np.sub.page', false)],
  array[true, false]);

select wiki_test.expect_eq('a nearer allow beats an inherited deny',
  array[wiki_test.carol_reads('root.ctx.deny.sub.allowed', false),
        wiki_test.carol_reads('root.ctx.deny.sub.page', false)],
  array[true, false]);

select wiki_test.expect_eq('deny wins at equal distance',
  wiki_test.carol_reads('root.ctx.tie', false), false);

-- The cut takes away what is above it, and leaves an explicit ACE below it.
select wiki_test.expect_eq('inheritance_blocked cuts the ancestors off',
  array[wiki_test.carol_reads('root.ctx.cut.open', false),
        wiki_test.carol_reads('root.ctx.cut.blocked', true),
        wiki_test.carol_reads('root.ctx.cut.blocked.page', false),
        wiki_test.carol_reads('root.ctx.cut.blocked.own', false)],
  array[true, false, false, true]);

-- And the other half: the blocked document keeps its own ACEs, and they still
-- reach what is under it. It is the ancestors that stop, not the document.
select wiki_test.expect_eq('a blocked folder still grants from its own ACE',
  array[wiki_test.carol_reads('root.ctx.blocked', true),
        wiki_test.carol_reads('root.ctx.blocked.page', false)],
  array[true, true]);

select wiki_test.expect_eq('an expired ACE grants nothing',
  wiki_test.carol_reads('root.ctx.expired.page', false), false);

select wiki_test.expect_eq('and is not in the context either',
  (select cardinality((wiki.acl_context('read', wiki_test.who('carol'))).sources))
  = (select count(*)::int from wiki.ace a
      where a.principal_id in (select principal_id
                                 from wiki.effective_principals(wiki_test.who('carol')))
        and (a.expires_at is null or a.expires_at > now())
        and wiki.ace_covers(a.role_id, 'read', a.ace_type)),
  true);

-- All eight capabilities at once, which is what wiki.current_document exposes
-- as `capabilities` and what the FUSE driver reads on every listing. The
-- contexts arrive as an array indexed by the enum's own order, so a pairing
-- mistake would answer each question with a different capability's ACL --
-- plausible-looking output, entirely wrong. This compares the whole array.
select wiki_test.expect_eq('capabilities_at_ctx agrees with capabilities_at',
  (select count(*)::int
     from wiki.document d
     cross join (select p.id from wiki.principal p where p.kind = 'user'
                  union all select null::uuid) u
    where wiki.capabilities_at(d.id, u.id)
      is distinct from wiki.capabilities_at_ctx(d.path, d.is_folder, d.owner_id,
                                                wiki.acl_contexts(u.id))), 0);

-- Not vacuous: somebody has to hold something, and somebody has to hold
-- everything, or the comparison above is two empty arrays.
select wiki_test.expect('the capability arrays are not all empty',
  (select count(*) from wiki.document d
     cross join (select p.id from wiki.principal p where p.kind = 'user') u
    where cardinality(wiki.capabilities_at_ctx(d.path, d.is_folder, d.owner_id,
                                               wiki.acl_contexts(u.id))) > 1) > 0);

------------------------------------------------------------------------------
-- 3. Traversal, against the definition it replaced
------------------------------------------------------------------------------
--
-- wiki.can_traverse() now builds a context once and tests descendants against
-- it. The rule it used to apply is written out here rather than kept as a
-- second function, because a rule with no caller rots.

select wiki_test.expect_eq('can_traverse agrees with the per-row walk it replaced',
  (select count(*)::int
     from wiki.document d
     cross join (select unnest(enum_range(null::wiki.capability)) as capability) c
     cross join (select p.id from wiki.principal p where p.kind = 'user'
                  union all select null::uuid) u
    where wiki.can_traverse(d.path, c.capability, u.id)
      is distinct from (
        wiki.is_superuser(u.id) or exists (
          select 1 from wiki.document child
           where child.path <@ d.path and child.path <> d.path
             and wiki.can(child.path, child.is_folder, child.owner_id,
                          c.capability, u.id)))), 0);

------------------------------------------------------------------------------
-- 4. And the policy itself, which is what any of this was for
------------------------------------------------------------------------------
--
-- Sections 1 and 3 prove the functions agree. This proves the *policy* built
-- from them admits the same rows the old one did, for every fixture user --
-- which is the only statement a reader of this wiki cares about.

create or replace function wiki_test.visible_by_the_old_rule(p_user uuid)
returns ltree[]
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select coalesce(array_agg(d.path order by d.path), '{}')
    from wiki.document d
   where wiki.can(d.path, d.is_folder, d.owner_id, 'read', p_user)
      or (d.is_folder and (
            wiki.is_superuser(p_user) or exists (
              select 1 from wiki.document child
               where child.path <@ d.path and child.path <> d.path
                 and wiki.can(child.path, child.is_folder, child.owner_id,
                              'read', p_user))));
$$;

create or replace function wiki_test.visible_by_the_policy()
returns ltree[]
language sql stable security invoker as $$
  select coalesce(array_agg(d.path order by d.path), '{}') from wiki.document d;
$$;

grant execute on function wiki_test.visible_by_the_policy() to fswiki_user;
grant execute on function wiki_test.carol_reads(text, boolean) to fswiki_user;

do $$
declare u text;
begin
  foreach u in array array['alice','bob','carol','dave','erin','frank','grace'] loop
    perform wiki_test.login(u);
    execute 'set role fswiki_user';
    perform wiki_test.expect_eq(
      format('%s sees exactly what the old rule admitted', u),
      wiki_test.visible_by_the_policy(),
      wiki_test.visible_by_the_old_rule(wiki_test.who(u)));
    execute 'reset role';
  end loop;
end;
$$;

-- The same for the mount's own view, which asks a different capability and
-- was rewritten the same way. `sync` is not `read`: frank holds a deny-sync,
-- so this is not the previous assertion in different words.
-- The base is current_document's, tombstones and all, and the caller has to
-- get past document_select first -- syncable_document is a view over a view,
-- and comparing it against the raw table would fail for reasons that have
-- nothing to do with the ACL. It did, the first time this was written.
create or replace function wiki.syncable_by_the_old_rule(p_user uuid)
returns ltree[]
language sql stable security definer
set search_path = wiki, public, pg_temp as $$
  select coalesce(array_agg(d.path order by d.path), '{}')
    from wiki.document d
    left join wiki.document_version v
      on v.document_id = d.id and upper_inf(v.valid)
   where not coalesce(v.is_tombstone, false)
     and (wiki.can(d.path, d.is_folder, d.owner_id, 'read', p_user)
          or (d.is_folder and (
                wiki.is_superuser(p_user) or exists (
                  select 1 from wiki.document c
                   where c.path <@ d.path and c.path <> d.path
                     and wiki.can(c.path, c.is_folder, c.owner_id, 'read', p_user)))))
     and (wiki.has_capability(d.id, 'sync', p_user)
          or (d.is_folder and (
                wiki.is_superuser(p_user) or exists (
                  select 1 from wiki.document c
                   where c.path <@ d.path and c.path <> d.path
                     and wiki.can(c.path, c.is_folder, c.owner_id, 'sync', p_user)))));
$$;

create or replace function wiki_test.syncable_by_the_view()
returns ltree[]
language sql stable security invoker as $$
  select coalesce(array_agg(d.path order by d.path), '{}')
    from wiki.syncable_document d;
$$;

grant execute on function wiki_test.syncable_by_the_view() to fswiki_user;

do $$
declare u text;
begin
  foreach u in array array['alice','bob','carol','dave','erin','frank','grace'] loop
    perform wiki_test.login(u);
    execute 'set role fswiki_user';
    perform wiki_test.expect_eq(
      format('%s may sync exactly what the old rule admitted', u),
      wiki_test.syncable_by_the_view(),
      wiki.syncable_by_the_old_rule(wiki_test.who(u)));
    execute 'reset role';
  end loop;
end;
$$;

-- And the caller with no token, who is `public` and nothing else.
select set_config('request.jwt.claims', '', false);
set role fswiki_anon;
select wiki_test.expect_eq('an anonymous caller sees exactly what the old rule admitted',
  wiki_test.visible_by_the_policy(),
  wiki_test.visible_by_the_old_rule(null));
reset role;

-- Not vacuous: the users must not all see the same thing, or the comparison
-- above is one answer repeated seven times.
select wiki_test.expect('the fixture users do not all see the same tree',
  (select count(distinct wiki_test.visible_by_the_old_rule(p.id))
     from wiki.principal p where p.kind = 'user') > 3);

------------------------------------------------------------------------------
-- 5. The context is not the ACL
------------------------------------------------------------------------------
--
-- A policy is checked against the querying role, so every function a policy
-- names is executable by a client -- and PostgREST exposes each of those as an
-- RPC. wiki.acl_context() is therefore something a caller can simply ask for,
-- and if it held paths it would hand them the list of pages hidden from them.
-- It holds sha256 of each path instead. This is that claim, asserted.

select wiki_test.expect_eq('no document path appears in a context',
  (select count(*)::int
     from wiki.document d
     cross join (select p.id from wiki.principal p where p.kind = 'user') u
    where wiki.acl_context('read', u.id)::text like '%' || d.path::text || '%'), 0);

-- The obvious way for that to pass by accident: a context with nothing in it.
select wiki_test.expect('a context does hold sources',
  (select cardinality((wiki.acl_context('read', wiki_test.who('carol'))).sources)) > 5);

-- The hash has to be the one can_ctx recomputes, or every answer is no.
select wiki_test.expect_eq('a source prefix is the key of the path it came from',
  (select count(*)::int from wiki.ace a
     join wiki.document d on d.id = a.document_id
    where a.principal_id = wiki_test.who('carol')
      and not exists (
        select 1 from unnest((wiki.acl_context('read', wiki_test.who('carol'))).sources) s
         where s.prefix = wiki.path_key(d.path))
      and wiki.ace_covers(a.role_id, 'read', a.ace_type)
      and (a.expires_at is null or a.expires_at > now())), 0);
