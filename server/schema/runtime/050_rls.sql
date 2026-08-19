-- 050_rls.sql, the runtime half.
-- The file header, and the reasoning, are in ../tables/050_rls.sql.

------------------------------------------------------------------------------
-- Documents
------------------------------------------------------------------------------

-- A folder is visible either because you may read it outright, or because it
-- holds something you may read. Without the second arm the tree comes back
-- disconnected and the mount is unusable. See wiki.can_traverse().
--
-- The sub-SELECT is the whole performance of reading this wiki, and it looks
-- like a mistake. It is not: `(select f())` with nothing correlated in it is an
-- InitPlan, which PostgreSQL evaluates **once per statement**. Written as a
-- bare `wiki.acl_context('read')` it would be a per-row call, and the caller's
-- ACL would be re-derived for every document in the answer -- which is what
-- this replaced. Measured on 1,021 documents: 494 us per document before, 6.1
-- us after. See wiki.acl_context() and issue #10.
--
-- The traversal arm still calls wiki.can_traverse(), whose signature and
-- grants are unchanged. It builds a context of its own, once per call, and
-- pays the same 14 us per descendant -- see the end of 040_authz.sql. Keeping
-- it out of the policy expression keeps a context-taking function out of
-- reach of a client, which matters: this one reads the tree, so a forged
-- context would turn it into an existence oracle over paths.
create policy document_select on wiki.document
  for select using (
    wiki.can_ctx(path, is_folder, owner_id, 'read',
                 (select wiki.acl_context('read')))
    or (is_folder and wiki.can_traverse(path, 'read'))
  );

-- Creating a document is a permission on its *parent* — the row being inserted
-- has no ACL of its own yet, only what it will inherit.
create policy document_insert on wiki.document
  for insert with check (wiki.has_capability(parent_id, 'create'));

-- USING gates which rows you may target; WITH CHECK gates the result. Both are
-- needed: without the CHECK a writer could re-parent a document into a subtree
-- they have no rights over, dragging its content with it.
create policy document_update on wiki.document
  for update using (wiki.can(path, is_folder, owner_id, 'write'))
          with check (wiki.can(path, is_folder, owner_id, 'write'));

-- Note the capability: a row DELETE is permanent destruction, so it needs
-- 'purge', not 'delete'. Retiring a document is a tombstone version written
-- through the push RPC and gated on 'delete'.
create policy document_delete on wiki.document
  for delete using (wiki.can(path, is_folder, owner_id, 'purge'));

------------------------------------------------------------------------------
-- Versions: history is exactly as readable as its document
------------------------------------------------------------------------------

-- Deliberately re-tests 'read' rather than leaning on document's own policy:
-- that policy also admits traversal-only folders, and content must not follow a
-- row in on those grounds.
create policy document_version_select on wiki.document_version
  for select using (wiki.has_capability(document_id, 'read'));

-- Publishing a revision. Three ways to be entitled to one, matching the three
-- things a revision can be:
--
--   an edit          -> 'write' on the document
--   a retirement     -> 'delete' on the document (no 'write' needed; see the
--                       capability lattice, where delete sits beside it)
--   a first revision -> 'create' on the containing folder, so that a principal
--                       holding bare `author` can actually fill in the document
--                       they were allowed to create
--
-- This is the security boundary for publishing. wiki.push() re-checks the same
-- conditions beforehand, but only so it can report a clean 'forbidden' status
-- instead of aborting the transaction — these policies are what enforce it.
create policy document_version_insert on wiki.document_version
  for insert with check (
    wiki.has_capability(document_id, 'write')
    or (is_tombstone and wiki.has_capability(document_id, 'delete'))
    or (version = 1 and wiki.has_capability(
          (select d.parent_id from wiki.document d where d.id = document_id), 'create'))
  );

-- Superseding: closing a live revision so the next may open. The
-- document_version_immutable trigger restricts *what* may change; this policy
-- restricts *who* may change it.
create policy document_version_supersede on wiki.document_version
  for update using (
    wiki.has_capability(document_id, 'write')
    or wiki.has_capability(document_id, 'delete')
  ) with check (
    wiki.has_capability(document_id, 'write')
    or wiki.has_capability(document_id, 'delete')
  );

------------------------------------------------------------------------------
-- Drafts: yours and nobody else's
------------------------------------------------------------------------------

create policy draft_all on wiki.draft
  for all using (author_id = wiki.current_user_id())
      with check (author_id = wiki.current_user_id());

------------------------------------------------------------------------------
-- ACEs: you may read the ACL of anything you can read, and edit the ACL of
-- anything you hold 'grant' on.
------------------------------------------------------------------------------

-- Seeing who else has access is a normal part of using a wiki ("who can see
-- this page?"), so read access to the document carries read access to its ACL.
-- Drop the 'read' arm if the ACL itself is considered sensitive; the 'grant'
-- arm must stay.
--
-- The 'grant' arm is not redundant. Postgres applies SELECT policies to the
-- rows an UPDATE or DELETE touches, so without it an owner locked out by a
-- deny-everything ACE would hold 'grant', be allowed to delete the offending
-- ACE by ace_delete, and still delete nothing — the row would be invisible.
-- The lockout escape only works if you can see what is blocking you.
create policy ace_select on wiki.ace
  for select using (
    wiki.has_capability(document_id, 'read')
    or wiki.has_capability(document_id, 'grant')
  );

create policy ace_insert on wiki.ace
  for insert with check (wiki.has_capability(document_id, 'grant'));

create policy ace_update on wiki.ace
  for update using (wiki.has_capability(document_id, 'grant'))
          with check (wiki.has_capability(document_id, 'grant'));

create policy ace_delete on wiki.ace
  for delete using (wiki.has_capability(document_id, 'grant'));

------------------------------------------------------------------------------
-- Directory: who exists, and which roles there are
------------------------------------------------------------------------------

-- You have to be able to name a principal in order to put them in an ACE, so
-- the directory is readable by any authenticated user. If the membership of a
-- group is itself sensitive, this is the policy to tighten.
create policy principal_select on wiki.principal
  for select using (wiki.current_user_id() is not null);

create policy user_account_select on wiki.user_account
  for select using (wiki.current_user_id() is not null);

create policy user_account_self_update on wiki.user_account
  for update using (principal_id = wiki.current_user_id())
          with check (principal_id = wiki.current_user_id());

create policy group_member_select on wiki.group_member
  for select using (wiki.current_user_id() is not null);

create policy role_select on wiki.role
  for select using (wiki.current_user_id() is not null);

create policy role_capability_select on wiki.role_capability
  for select using (wiki.current_user_id() is not null);

create policy role_inherits_select on wiki.role_inherits
  for select using (wiki.current_user_id() is not null);

create policy capability_requires_select on wiki.capability_requires
  for select using (wiki.current_user_id() is not null);
