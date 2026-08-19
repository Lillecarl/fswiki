-- wiki.ace_closure: the precomputed role/capability closure.
--
-- Materialising an answer the ACL used to compute is a performance change with
-- a security failure mode. A stale closure does not make the wiki slow; it
-- makes it wrong, in whichever direction the drift went — a row that should
-- not be there grants access nobody was given, and a row missing takes access
-- away from somebody who was.
--
-- So there are two things to prove, and neither is a spot check.
--
--   1. The table says exactly what the recursive definition says. Over the
--      whole cross product, not over a sample.
--   2. It tracks the inputs when they move. A rebuild in seed/ alone is not
--      enough, and that is measured rather than argued: 010_fixtures.sql
--      creates a `retirer` role after the seed has run, and with the seed call
--      alone the closure held nothing for it. Frank silently lost the `delete`
--      he had been granted, and 050_merge_test.sql failed three files later
--      with an error naming neither the role nor the closure.
--
--   3. No client role can write either the inputs or the closure. That is the
--      property that keeps a rebuild honest, and it belongs to the *grants*
--      rather than to the code — which is why it is asserted here.

------------------------------------------------------------------------------
-- 1. The closure is the definition, over every combination there is
------------------------------------------------------------------------------

-- wiki.ace_covers_uncached() is the recursive form kept for exactly this.
-- Every role x every capability x every ace_type: the table and the walk must
-- agree on all of them, in both directions of disagreement.
select wiki_test.expect_eq('the closure agrees with the recursive definition',
  (select count(*)::int
     from wiki.role r
     cross join (select unnest(enum_range(null::wiki.capability)) as capability) c
     cross join (select unnest(enum_range(null::wiki.ace_type))   as ace_type) t
    where wiki.ace_covers_uncached(r.id, c.capability, t.ace_type)
      is distinct from exists (
        select 1 from wiki.ace_closure x
         where x.role_id = r.id and x.capability = c.capability
           and x.ace_type = t.ace_type)), 0);

-- And that the two functions agree, which is the thing every policy calls.
select wiki_test.expect_eq('ace_covers agrees with ace_covers_uncached',
  (select count(*)::int
     from wiki.role r
     cross join (select unnest(enum_range(null::wiki.capability)) as capability) c
     cross join (select unnest(enum_range(null::wiki.ace_type))   as ace_type) t
    where wiki.ace_covers(r.id, c.capability, t.ace_type)
      is distinct from wiki.ace_covers_uncached(r.id, c.capability, t.ace_type)), 0);

-- Not empty, and not everything. Both are ways for the two tests above to pass
-- while the wiki is broken: an empty closure refuses every read, and a full one
-- makes every role cover every capability.
select wiki_test.expect('the closure is neither empty nor complete',
  (select count(*) from wiki.ace_closure) > 0
  and (select count(*) from wiki.ace_closure)
      < (select count(*) from wiki.role)
        * (select count(*) from unnest(enum_range(null::wiki.capability)))
        * (select count(*) from unnest(enum_range(null::wiki.ace_type))),
  (select count(*)::text from wiki.ace_closure) || ' rows');

-- A spot check that says the direction is not reversed, which is the mistake
-- the cross-product tests above cannot see: they would pass just as happily
-- against a closure built with allow and deny swapped, because it would still
-- match a definition built the same wrong way. These read from the schema's
-- own intent instead. `reader` grants `read`; it does not grant `write`.
select wiki_test.expect('an allow-reader covers read',
  wiki.ace_covers((select id from wiki.role where name = 'reader'), 'read', 'allow'));

select wiki_test.expect('an allow-reader does not cover write',
  not wiki.ace_covers((select id from wiki.role where name = 'reader'), 'write', 'allow'));

-- The asymmetry that the two closure directions exist for. Denying `read`
-- must take `write` with it -- you cannot write what you may not read -- while
-- allowing `read` must not hand out `write`.
select wiki_test.expect('a deny-reader reaches write, upward',
  wiki.ace_covers((select id from wiki.role where name = 'reader'), 'write', 'deny'));

------------------------------------------------------------------------------
-- 2. It tracks the inputs when they move
------------------------------------------------------------------------------

-- A role created after the seed ran. This is not hypothetical: `retirer`,
-- three lines into the ACL fixtures, is exactly this, and it is what proved a
-- seed-time rebuild insufficient.
select wiki_test.expect('a role created after the seed is in the closure',
  wiki.ace_covers(wiki.role_id('retirer'), 'delete', 'allow'));

select wiki_test.expect('and does not cover what it was not given',
  not wiki.ace_covers(wiki.role_id('retirer'), 'write', 'allow'));

-- Each of the four inputs, moved, and the closure following. Rolled back, so
-- the assertions below still see the schema the rest of the suite does.
begin;
  insert into wiki.role (name, description) values ('closure_probe', 'temporary');
  select wiki_test.expect('a brand new role covers nothing yet',
    not wiki.ace_covers(wiki.role_id('closure_probe'), 'read', 'allow'));

  insert into wiki.role_capability (role_id, capability)
  values (wiki.role_id('closure_probe'), 'read');
  select wiki_test.expect('granting it a capability reaches the closure',
    wiki.ace_covers(wiki.role_id('closure_probe'), 'read', 'allow'));

  insert into wiki.role_inherits (role_id, inherits_role_id)
  values (wiki.role_id('closure_probe'), wiki.role_id('editor'));
  select wiki_test.expect('and so does inheriting one',
    wiki.ace_covers(wiki.role_id('closure_probe'), 'write', 'allow'));

  delete from wiki.role_capability where role_id = wiki.role_id('closure_probe');
  delete from wiki.role_inherits where role_id = wiki.role_id('closure_probe');
  select wiki_test.expect('taking them away reaches it too',
    not wiki.ace_covers(wiki.role_id('closure_probe'), 'read', 'allow'));

  delete from wiki.role where name = 'closure_probe';
  select wiki_test.expect_eq('and dropping the role leaves nothing behind',
    (select count(*)::int from wiki.ace_closure x
      join wiki.role r on r.id = x.role_id where r.name = 'closure_probe'), 0);
rollback;

-- The closure is still what it was, after all that.
select wiki_test.expect_eq('the closure survives the probe unchanged',
  (select count(*)::int
     from wiki.role r
     cross join (select unnest(enum_range(null::wiki.capability)) as capability) c
     cross join (select unnest(enum_range(null::wiki.ace_type))   as ace_type) t
    where wiki.ace_covers_uncached(r.id, c.capability, t.ace_type)
      is distinct from wiki.ace_covers(r.id, c.capability, t.ace_type)), 0);

------------------------------------------------------------------------------
-- 3. No client role can write the inputs or the closure
------------------------------------------------------------------------------
--
-- The rebuild is only as honest as the inputs. A client that could insert a
-- role_capability row could grant itself a capability; one that could insert
-- into the closure directly could skip the rules entirely.
--
-- Checked against information_schema rather than against the grant files,
-- because the question is what the database will permit, not what the
-- repository meant to permit.

select wiki_test.expect_eq(
  'no role may write the tables the closure is derived from',
  (select count(*)::int
     from information_schema.role_table_grants g
    where g.table_schema = 'wiki'
      and g.table_name in ('role', 'role_capability', 'role_inherits',
                           'capability_requires')
      and g.privilege_type in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
      and g.grantee in ('fswiki_user', 'fswiki_anon', 'fswiki_authenticator',
                        'PUBLIC')), 0);

-- The closure itself, likewise. It is read by a security definer function as
-- the owner, so no client role needs any privilege on it at all -- and one
-- with INSERT could grant itself every capability there is.
select wiki_test.expect_eq('no client role may touch the closure',
  (select count(*)::int
     from information_schema.role_table_grants g
    where g.table_schema = 'wiki' and g.table_name = 'ace_closure'
      and g.grantee in ('fswiki_user', 'fswiki_anon', 'fswiki_authenticator',
                        'PUBLIC')), 0);
