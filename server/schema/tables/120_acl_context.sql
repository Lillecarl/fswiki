-- The reader's ACL, as data a policy can carry.
--
-- wiki.can() answers one question about one document, and a SELECT over a tree
-- asks it once per row. Every one of those calls re-derives the same things:
-- which principals the caller counts as, which ACEs name them, and which of
-- those speak to the capability. On a thousand documents that is a thousand
-- identical derivations, and it was 445 us per document -- measured, and
-- linear in the tree. See issue #10.
--
-- These types let the derivation happen once. wiki.acl_context() builds one,
-- an uncorrelated sub-SELECT in the policy makes PostgreSQL evaluate it as an
-- InitPlan (once per statement, confirmed in EXPLAIN), and wiki.can_ctx()
-- answers from it without reading a table at all.
--
-- The types live here rather than in runtime/ because runtime/ is dropped and
-- replayed on every start, and a composite type cannot be dropped while a
-- function signature mentions it.
--
-- WHY THERE ARE NO PATHS IN HERE
-- -----------------------------
-- A policy is evaluated with the *querying* role's privileges, so every
-- function a policy names must be executable by that role -- and PostgREST
-- exposes every executable function as an RPC. So whatever wiki.acl_context()
-- returns, a client can ask for directly.
--
-- Returning the ACEs with their paths would therefore hand a caller the list
-- of documents that carry a deny naming them: the paths of the pages hidden
-- from them, without having to guess a single one. That is the one property
-- this project will not trade -- a page you may not read is indistinguishable
-- from a page that is not there.
--
-- So a source carries `sha256` of the path it sits on rather than the path.
-- Containment becomes equality against the hash of the target's prefix at the
-- same depth, which is the same answer. What a caller gets from the RPC is a
-- list of depths and flags: enough to confirm a path they already guessed --
-- which wiki.can() already answers for any path they can name -- and not
-- enough to enumerate one they have not.

-- One ACE, flattened onto the document it sits on.
--
-- `depth` is nlevel() of that document's path. It is carried rather than
-- derived because it decides the winner: nearest ancestor wins, and nearest
-- is deepest.
create type wiki.acl_source as (
  prefix            bytea,
  depth             integer,
  ace_type          wiki.ace_type,
  inherit_only      boolean,
  container_inherit boolean,
  object_inherit    boolean,
  no_propagate      boolean
);

-- One inheritance_blocked document, the same way.
create type wiki.acl_cut as (
  prefix bytea,
  depth  integer
);

-- Everything one caller's answer depends on, for one capability.
--
--   is_superuser  short-circuits the whole ACL, so it is asked once
--   principals    the caller's effective principals, for the owner's grant
--   sources       every ACE that names them and speaks to the capability
--   cuts          the inheritance_blocked documents that can change an answer
--
-- `cuts` is filtered rather than complete, and the filter is part of the rule
-- rather than an optimisation. A block at B only removes sources above B; if
-- no source is above B it removes nothing, because any source that applies to
-- a document under B is an ancestor of that document and so comparable with B.
-- Without the filter, a wiki that blocks inheritance on every folder would
-- carry every folder in here.
create type wiki.acl_context as (
  is_superuser boolean,
  principals   uuid[],
  sources      wiki.acl_source[],
  cuts         wiki.acl_cut[]
);

-- Blocked documents are rare and are found by containment, so this is the
-- index wiki.acl_context() needs to avoid a sequential scan for them.
create index document_blocked_gist_idx on wiki.document using gist (path)
  where inheritance_blocked;
