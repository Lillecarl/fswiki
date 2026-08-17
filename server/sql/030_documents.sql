-- Documents, versions, and per-user drafts.
--
-- `document` holds one row per live path (a page or a folder). History lives in
-- `document_version`; `document.current_version_id` is the published tip. A
-- client's uncommitted work lives in `draft` and is visible only to its author
-- until pushed.
--
-- Paths are materialised as ltree so that a grant on a subtree is one indexable
-- `<@` test. Every path starts with the literal label `root`.

create table wiki.document (
  id          uuid primary key default gen_random_uuid(),
  parent_id   uuid references wiki.document(id) on delete restrict,
  slug        text not null,
  -- Denormalised from parent_id + slug by trigger; the thing RLS actually tests.
  path        ltree not null,
  is_folder   boolean not null default false,
  title       text,

  -- There is deliberately no current_version_id. The live version is the one
  -- whose validity interval is still open; see document_version.valid. A cached
  -- pointer would be a second source of truth that can drift from the first.

  -- The owner always retains 'grant', whatever the ACL says. Without this an
  -- unlucky deny ACE locks a document so that nobody — including the person who
  -- made the mistake — can repair its permissions. Windows solves it the same
  -- way, and it is not optional.
  owner_id    uuid references wiki.principal(id) on delete set null,

  -- Windows' "disable inheritance" / protected DACL. When true, ACEs from
  -- ancestors stop here and do not reach this document or anything below it.
  inheritance_blocked boolean not null default false,

  created_at  timestamptz not null default now(),
  created_by  uuid references wiki.principal(id) on delete set null,
  updated_at  timestamptz not null default now(),

  -- ltree labels on PG16+ accept hyphens and non-ASCII, so slugs can stay
  -- human-readable. They may not contain the ltree separator, a path
  -- separator, or whitespace (all three would confuse either ltree or FUSE).
  -- octet_length, not length: NAME_MAX is 255 *bytes*, and ltree labels accept
  -- non-ASCII. Counting characters lets a 255-character CJK slug through at 765
  -- bytes, which the kernel then refuses to create — a document the wiki holds
  -- but no client can ever materialise.
  constraint document_slug_shape
    check (slug ~ '^[^./\\[:space:]]+$' and octet_length(slug) <= 255),
  constraint document_path_rooted check (path <@ 'root'::ltree),
  constraint document_path_key    unique (path),
  constraint document_sibling_key unique (parent_id, slug),
  -- Exactly one root, and only the root may be parentless.
  constraint document_root_shape
    check ((parent_id is null) = (path = 'root'::ltree))
);

create index document_path_gist_idx on wiki.document using gist (path);
create index document_parent_idx    on wiki.document (parent_id);

-- Versioning is full snapshots in a temporal table.
--
-- `version` is the human-facing revision number ("revision 7"). `valid` is the
-- wall-clock interval during which that revision was the published one, which
-- is what makes point-in-time reads possible: the wiki as of any timestamp is
-- one predicate, `valid @> t`, across every document at once. Superseding a
-- revision closes its interval and opens the next in the same transaction.
--
-- Two constraints keep that honest, and both are enforced by the database
-- rather than by whichever client happens to be writing:
--
--   * the EXCLUDE constraint forbids two revisions of one document from being
--     live at overlapping times (this is what btree_gist is for — it lets one
--     GiST index mix uuid equality with range overlap);
--   * the partial unique index forbids two open-ended revisions, so there is
--     exactly one current version and no cached pointer can disagree with it.
--
-- Snapshots rather than deltas, on purpose. Markdown is small, TOAST already
-- compresses it, and reconstructing content from a delta chain on every read is
-- the beginning of writing a version control system — which is the thing this
-- design is trying not to do. If storage ever bites, dedupe by content_hash
-- into a blob table before reaching for deltas.
create table wiki.document_version (
  id           uuid primary key default gen_random_uuid(),
  document_id  uuid not null references wiki.document(id) on delete cascade,
  version      integer not null,
  -- The path this version was published at, so a rename is visible in history.
  path         ltree not null,
  content      text,
  content_type text not null default 'text/markdown',
  -- sha256 of content, for the client's sync diff and for dedupe later.
  content_hash bytea generated always as (digest(coalesce(content, ''), 'sha256')) stored,

  -- A retirement. The document row and its history stay; only the current view
  -- drops it. Reinstating is another version, not an undelete.
  is_tombstone boolean not null default false,

  message      text,
  author_id    uuid references wiki.principal(id) on delete set null,
  created_at   timestamptz not null default now(),
  valid        tstzrange not null default tstzrange(now(), null),
  parent_version_id uuid references wiki.document_version(id) on delete set null,

  constraint document_version_key unique (document_id, version),
  constraint document_version_positive check (version > 0),
  constraint document_version_tombstone_empty
    check (not is_tombstone or content is null),
  -- An empty interval would be a revision that was never live, and would slip
  -- past both the exclusion constraint and upper_inf().
  constraint document_version_valid_nonempty check (not isempty(valid)),
  constraint document_version_no_overlap
    exclude using gist (document_id with =, valid with &&)
);

create index document_version_document_idx
  on wiki.document_version (document_id, version desc);

-- Exactly one live revision per document.
create unique index document_version_current_idx
  on wiki.document_version (document_id) where upper_inf(valid);

-- Point-in-time lookups across the whole wiki.
create index document_version_valid_idx
  on wiki.document_version using gist (valid);

-- History is immutable except for one transition: closing a live revision when
-- the next one supersedes it. A trigger rather than an RLS policy because
-- WITH CHECK cannot see the OLD row, and because triggers bind the table owner
-- too — this invariant should hold for migrations and admin sessions as much as
-- for clients.
create or replace function wiki.document_version_only_close()
returns trigger
language plpgsql as $$
begin
  if new.id           is distinct from old.id
  or new.document_id  is distinct from old.document_id
  or new.version      is distinct from old.version
  or new.path         is distinct from old.path
  or new.content      is distinct from old.content
  or new.content_type is distinct from old.content_type
  or new.is_tombstone is distinct from old.is_tombstone
  or new.author_id    is distinct from old.author_id
  or lower(new.valid) is distinct from lower(old.valid)
  then
    raise exception 'revisions are immutable; only the validity interval may be closed'
      using errcode = 'restrict_violation';
  end if;

  if not upper_inf(old.valid) then
    raise exception 'revision % is already closed', old.version
      using errcode = 'restrict_violation';
  end if;

  return new;
end;
$$;

create trigger document_version_immutable
  before update on wiki.document_version
  for each row execute function wiki.document_version_only_close();

------------------------------------------------------------------------------
-- Drafts
------------------------------------------------------------------------------

create type wiki.draft_op as enum ('create', 'update', 'delete', 'move');

-- Whether a draft is ready to publish or is mid-merge.
--
-- Deliberately a flag the client sets, not something inferred from the text.
-- Grepping for conflict markers is how a client decides a merge is *resolved*,
-- but it is a terrible way to decide one is *outstanding*: a page documenting a
-- merge tool contains markers and must stay publishable, and a user who deletes
-- the markers without choosing a side has resolved nothing. The flag says what
-- happened; the text says what it looks like now.
create type wiki.draft_state as enum ('clean', 'conflicted');

-- A draft is one pending change by one author. The FUSE client composites these
-- over the published tree so an author sees their own edits in place.
create table wiki.draft (
  id           uuid primary key default gen_random_uuid(),
  author_id    uuid not null references wiki.principal(id) on delete cascade,
  operation    wiki.draft_op not null,

  -- Null for 'create': the document does not exist server-side yet.
  document_id  uuid references wiki.document(id) on delete cascade,
  -- Target path. Differs from document.path exactly when operation = 'move'.
  path         ltree not null,
  content      text,
  content_type text not null default 'text/markdown',
  message      text,

  -- The published version this edit was based on. The push transaction refuses
  -- the changeset if the document has moved past it. Null only for 'create'.
  base_version integer,

  -- Merge bookkeeping. A merge rewrites `content` in place — that is what makes
  -- the result visible through the mount with no special case in the client —
  -- so the text it replaced is kept here, and restoring it is how a user backs
  -- out. Set by any merge, clean or not: a clean merge still rewrote work the
  -- author had not published, and they are entitled to change their mind.
  pre_merge_content text,
  -- The revision the merge pulled in, so a resolved draft can be rebased onto
  -- it without asking the server what it was at the time.
  merged_from  integer,
  state        wiki.draft_state not null default 'clean',

  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  constraint draft_author_path_key unique (author_id, path),
  constraint draft_path_rooted check (path <@ 'root'::ltree),
  -- The two merge columns travel together, and a conflict cannot exist without
  -- something to go back to.
  constraint draft_merge_shape check (
    (pre_merge_content is null) = (merged_from is null)
    and (state <> 'conflicted' or pre_merge_content is not null)
  ),
  constraint draft_shape check (
    case operation
      when 'create' then document_id is null and base_version is null and content is not null
      when 'update' then document_id is not null and base_version is not null and content is not null
      when 'delete' then document_id is not null and base_version is not null
      when 'move'   then document_id is not null and base_version is not null
    end
  )
);

create index draft_author_idx   on wiki.draft (author_id);
create index draft_document_idx on wiki.draft (document_id) where document_id is not null;

------------------------------------------------------------------------------
-- Path maintenance
------------------------------------------------------------------------------

-- Keep document.path consistent with parent_id + slug, and cascade to the
-- subtree on rename/move. Doing this in a trigger means the application can
-- never write an inconsistent path, whichever client it came from.
create or replace function wiki.document_sync_path()
returns trigger
language plpgsql as $$
declare
  parent_path ltree;
  new_path    ltree;
begin
  if new.parent_id is null then
    new_path := 'root'::ltree;
  else
    select d.path into parent_path from wiki.document d where d.id = new.parent_id;
    if parent_path is null then
      raise exception 'parent document % does not exist', new.parent_id
        using errcode = 'foreign_key_violation';
    end if;
    new_path := parent_path || new.slug::ltree;
  end if;

  if tg_op = 'UPDATE' and new_path is distinct from old.path then
    -- Re-parent the subtree. `@>` is strict-ancestor-or-self, so exclude self.
    update wiki.document d
       set path = new_path || subpath(d.path, nlevel(old.path))
     where d.path <@ old.path and d.id <> new.id;
  end if;

  new.path := new_path;
  new.updated_at := now();
  return new;
end;
$$;

create trigger document_path_sync
  before insert or update of parent_id, slug on wiki.document
  for each row execute function wiki.document_sync_path();

-- A folder may not be deleted while it still has children; `on delete restrict`
-- on parent_id covers that. Moving a document under its own descendant would
-- create a cycle, which the path rewrite above cannot express.
create or replace function wiki.document_reject_cycle()
returns trigger
language plpgsql as $$
begin
  if new.parent_id is not null
     and exists (select 1 from wiki.document d
                  where d.id = new.parent_id and d.path <@ old.path) then
    raise exception 'cannot move % beneath its own descendant', old.path
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger document_no_cycles
  before update of parent_id on wiki.document
  for each row execute function wiki.document_reject_cycle();
