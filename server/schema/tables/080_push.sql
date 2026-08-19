

-- Publishing: promoting a user's drafts to published revisions, atomically.
--
-- SECURITY MODEL
-- --------------
-- Everything here runs SECURITY INVOKER, so RLS applies exactly as it does to
-- any other client statement, and the policies in 050_rls.sql are what actually
-- enforce access. The wiki.has_capability() calls in the validation pass are
-- *reporting*, not enforcement: they exist so a caller gets a clean 'forbidden'
-- row back instead of an aborted transaction. Removing them would make the
-- errors worse, not the system less safe.
--
-- The temporal invariants are held by the database, not by this function:
--
--   * the exclusion constraint forbids overlapping validity intervals;
--   * the partial unique index forbids two live revisions;
--   * the document_version_immutable trigger allows exactly one kind of update,
--     closing a live revision.
--
-- What this function adds on top is conflict detection and atomic grouping.
-- A client that bypasses it can still only clobber an edit, never corrupt the
-- history or escape the ACL.
--
-- ALL OR NOTHING
-- --------------
-- Validation runs over the whole changeset before anything is written. If any
-- entry comes back with a status other than 'published', **nothing was applied**
-- and the drafts are left intact for the client to resolve and retry. Clients
-- must check every row, not just the first. Note this is a product decision —
-- a commit should be atomic the way `svn commit` is — not something the
-- versioning model requires: each document's revision chain is independent.

create type wiki.push_status as enum (
  'published',  -- applied
  'conflict',   -- the document moved on beneath the draft; server state returned
  'unmerged',   -- the draft is mid-merge and the author has not finished
  'forbidden',  -- the caller lacks the capability this operation needs
  'missing',    -- the document, or the destination folder, is not there
  'invalid'     -- the draft cannot be applied at all (bad shape, folder op, ...)
);

create type wiki.push_result as (
  path            ltree,
  operation       wiki.draft_op,
  status          wiki.push_status,
  version         integer,   -- the revision published, when status = 'published'
  server_version  integer,   -- what the server currently holds, on conflict
  server_hash     bytea,
  server_content  text,      -- 'theirs', so the client can merge without another round trip
  -- 'base': the revision the draft says it descends from. A three-way merge
  -- needs all three sides, and this is the only one the client cannot
  -- reconstruct — it has its own text, and server_content above gives it
  -- theirs, but the common ancestor is a revision that is no longer live and
  -- may never have been on this machine at all. Storing full checkouts is what
  -- makes handing it back a lookup rather than a replay.
  --
  -- Null when there is no ancestor to speak of: a 'create' that collided with
  -- an existing path never descended from anything.
  base_content    text,
  detail          text
);
