

-- Views the clients read from.
--
-- security_invoker is essential: without it a view runs as its owner, who owns
-- the underlying tables and therefore bypasses RLS entirely. A view is the
-- easiest way in Postgres to accidentally hand out everything.

-- The published tip of every document the caller may read, content included, in
-- one round trip. This is the sync client's read query.
--
-- "Tip" means the revision whose validity interval is still open. Retired
-- documents drop out here while keeping their rows and history; folders have no
-- revisions at all and come through with a null version.
create view wiki.current_document
  with (security_invoker = true) as
  select d.id,
         -- parent_id so a client can assemble the tree without parsing ltree.
         d.parent_id,
         d.path,
         d.slug,
         d.is_folder,
         d.title,
         d.owner_id,
         d.inheritance_blocked,
         d.updated_at,
         v.id   as version_id,
         v.version,
         -- Who published the tip. A sync client needs this to tell "someone
         -- moved past me" from "my own push landed": the first is a conflict in
         -- waiting, the second means its working copy is already current.
         v.author_id as version_author_id,
         v.content,
         -- A binary body travels beside the text one rather than instead of
         -- it, so a caller asking for `content` gets null for a picture rather
         -- than something that decodes to nonsense. Exactly one is ever set;
         -- see tables/150_binary_versions.sql.
         v.content_bytes,
         -- Which of the two kinds this revision is, without fetching either.
         -- A folder and a tombstone are both false: neither has a body.
         (v.content_bytes is not null or v.storage <> 'database') as is_binary,
         -- Where the bytes are, for a client that can fetch them itself.
         -- 'database' means the two columns above; anything else means the
         -- locator, and wiki.storage_backend says what the name is.
         v.storage,
         v.locator,
         -- Byte length, so a client can stat a file without fetching it. FUSE
         -- needs st_size on every getattr and would otherwise pull the whole
         -- body just to list a directory. Stored rather than measured now:
         -- a body in a bucket has nothing here to measure.
         v.byte_size as size,
         v.content_type,
         v.content_hash,
         v.created_at as version_created_at,
         -- Eight questions per row, so the contexts are built once for the
         -- statement rather than once per row. Same InitPlan as
         -- document_select; see the end of 040_authz.sql.
         wiki.capabilities_at_ctx(d.path, d.is_folder, d.owner_id,
                                  (select wiki.acl_contexts())) as capabilities
    from wiki.document d
    left join wiki.document_version v
      on v.document_id = d.id and upper_inf(v.valid)
   where not coalesce(v.is_tombstone, false);

comment on view wiki.current_document is
  'Published tip plus the caller''s effective capabilities, for the sync client '
  'and for the xattrs the FUSE driver exposes.';

-- What the FUSE driver and the CLI are allowed to copy to local disk.
--
-- `sync` is deliberately separate from `read`. Denying it leaves a document
-- perfectly readable in the browser while keeping it off laptops: every view
-- then costs the reader a request the server can log, instead of one bulk sync
-- followed by silence. It is an audit lever, not a confidentiality one — anyone
-- who can read a page can still copy its text out by hand.
--
-- Folders appear when they lead somewhere syncable, on the same reasoning as
-- wiki.can_traverse() for reads: a mount with holes in its path is unusable.
--
-- It asks by path rather than by id, which is the same question -- the id
-- resolves to the path -- and lets the context be built once for the statement
-- instead of an ACL walk per row. See document_select in 050_rls.sql.
create view wiki.syncable_document
  with (security_invoker = true) as
  select d.*
    from wiki.current_document d
   where wiki.can_ctx(d.path, d.is_folder, d.owner_id, 'sync',
                      (select wiki.acl_context('sync')))
      or (d.is_folder and wiki.can_traverse(d.path, 'sync'));

comment on view wiki.syncable_document is
  'The subtree a client may mirror locally. Always a subset of what RLS lets the '
  'caller read, because `sync` requires `read`. Binary revisions are in it: a '
  'file is a revision, so a mirror gets one the way it gets a page.';

-- The wiki as it stood at some instant. Same shape as current_document, so a
-- history browser can render an old revision with the code that renders a live
-- one. RLS applies the *caller''s present-day* permissions to past content,
-- which is the safe direction: losing access hides history too.
create or replace function wiki.document_as_of(p_at timestamptz)
returns table (
  id uuid, path ltree, slug text, is_folder boolean, title text,
  version_id uuid, version integer, content text, content_bytes bytea,
  content_type text, content_hash bytea, version_created_at timestamptz
)
language sql stable parallel safe as $$
  select d.id, d.path, d.slug, d.is_folder, d.title,
         v.id, v.version, v.content, v.content_bytes, v.content_type,
         v.content_hash, v.created_at
    from wiki.document d
    left join wiki.document_version v
      on v.document_id = d.id and v.valid @> p_at
   where not coalesce(v.is_tombstone, false);
$$;

comment on function wiki.document_as_of(timestamptz) is
  'Point-in-time view of the wiki. Not SECURITY DEFINER: it reads through RLS, '
  'so the caller sees only what they may read today.';

grant select on wiki.current_document, wiki.syncable_document to fswiki_user;

-- The same two views for an unauthenticated caller. Both are
-- security_invoker, so RLS is evaluated against fswiki_anon and the rows that
-- come back are the ones granted to `public` -- the view adds no visibility of
-- its own, which is the entire reason for that setting.
grant select on wiki.current_document, wiki.syncable_document to fswiki_anon;

grant execute on function wiki.document_as_of(timestamptz) to fswiki_user;
