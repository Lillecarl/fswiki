-- The role/capability closure, precomputed.
--
-- `wiki.ace_covers()` answers one question: does an ACE carrying this role
-- cover this capability, in this direction? The answer is derived from three
-- tables that together hold 22 rows -- role_capability, role_inherits and
-- capability_requires -- by walking two recursive CTEs.
--
-- It was doing that from scratch about fourteen times per document, on every
-- document, on every request. Measured: 0.089 ms a call, 1.1 ms per document
-- inside resolve_ace, and 1.19 ms of the 1.19 ms that wiki.can() costs. That
-- is the whole of a page. See issue #10.
--
-- The closure of all of it is 75 rows.
--
-- Why materialising it is safe, and it is the only reason: **none of the three
-- source tables is writable over the API.** 060_roles.sql grants `select` on
-- them and nothing else, so they change in a schema migration and at no other
-- time -- and a migration replays seed/ and rebuilds this. A stale closure is
-- a wrong permission, so that property is asserted in
-- server/test/080_closure_test.sql rather than trusted.
--
-- Here in tables/ and not in runtime/ for two reasons that both bind. The
-- runtime track may not define a table (test_no_table_object_is_defined_in_the
-- _runtime_track), and wiki.ace_covers() is created in runtime/ and has to be
-- able to name this relation when it is. The *contents* are seed/, which is
-- where they can be: seed loads after runtime, so the roles this is computed
-- from exist by then.
--
-- No grants. wiki.ace_covers() is security definer and reads it as the owner,
-- so no client role needs to see this at all -- which keeps the allow-list in
-- server/test/070_public_test.sql exactly as it was.
create table if not exists wiki.ace_closure (
  role_id     uuid not null references wiki.role(id) on delete cascade,
  capability  wiki.capability not null,
  ace_type    wiki.ace_type not null,
  primary key (role_id, capability, ace_type)
);
