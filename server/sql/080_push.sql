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

------------------------------------------------------------------------------
-- Primitives
------------------------------------------------------------------------------

-- Close the live revision and open the next one, in one statement pair.
--
-- p_base_version is the revision the caller believes is live; the function
-- refuses if the server has moved past it. That check is what makes this safe
-- to expose directly — a client calling it instead of wiki.push() still cannot
-- silently overwrite someone else's edit, it just loses the batching.
create or replace function wiki.publish_revision(
  p_document     uuid,
  p_base_version integer,
  p_path         ltree,
  p_content      text,
  p_content_type text,
  p_message      text,
  p_tombstone    boolean default false
)
returns integer
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  v_prev   wiki.document_version;
  v_next   integer;
  v_author uuid := wiki.current_user_id();
begin
  if v_author is null then
    raise exception 'publishing requires an authenticated caller'
      using errcode = 'insufficient_privilege';
  end if;

  select * into v_prev
    from wiki.document_version
   where document_id = p_document and upper_inf(valid);

  if v_prev.version is distinct from p_base_version then
    raise exception 'document % is at revision %, not %',
      p_document, coalesce(v_prev.version::text, 'none'),
      coalesce(p_base_version::text, 'none')
      using errcode = 'serialization_failure';
  end if;

  if v_prev.id is not null then
    -- A revision opened in this same transaction would be closed to an empty
    -- interval, which the check constraint rejects. Better a clear error here
    -- than a confusing constraint violation two frames down.
    if lower(v_prev.valid) >= now() then
      raise exception
        'revision % of document % was opened in this transaction and cannot be superseded',
        v_prev.version, p_document
        using errcode = 'invalid_parameter_value';
    end if;

    update wiki.document_version
       set valid = tstzrange(lower(valid), now())
     where id = v_prev.id;
  end if;

  select coalesce(max(version), 0) + 1 into v_next
    from wiki.document_version where document_id = p_document;

  insert into wiki.document_version
    (document_id, version, path, content, content_type,
     is_tombstone, message, author_id, parent_version_id)
  values
    (p_document, v_next, p_path, p_content, coalesce(p_content_type, 'text/markdown'),
     p_tombstone, p_message, v_author, v_prev.id);

  return v_next;
end;
$$;

-- Create any missing folders along a path and return the deepest one. Folders
-- carry no revisions, so this is a plain insert; ACEs inherit by path, which is
-- why a new folder needs no ACL of its own.
create or replace function wiki.ensure_folder(p_path ltree)
returns uuid
language plpgsql volatile
set search_path = wiki, public, pg_temp as $$
declare
  v_id     uuid;
  v_parent uuid;
  v_prefix ltree;
  i        integer;
  v_author uuid := wiki.current_user_id();
begin
  for i in 1..nlevel(p_path) loop
    v_prefix := subpath(p_path, 0, i);
    select id into v_id from wiki.document where path = v_prefix;

    if v_id is null then
      insert into wiki.document (parent_id, slug, is_folder, title, owner_id, created_by)
      values (v_parent, subpath(p_path, i - 1, 1)::text, true,
              subpath(p_path, i - 1, 1)::text, v_author, v_author)
      returning id into v_id;
    end if;

    v_parent := v_id;
  end loop;

  return v_id;
end;
$$;

-- The deepest ancestor of a path that the caller can see. Always finds
-- something as long as the caller can see the root; a folder hidden by RLS is
-- correctly treated as not existing.
create or replace function wiki.nearest_existing_ancestor(p_path ltree)
returns uuid
language sql stable
set search_path = wiki, public, pg_temp as $$
  select d.id
    from wiki.document d
   where d.path @> p_path and d.path <> p_path
   order by nlevel(d.path) desc
   limit 1;
$$;

------------------------------------------------------------------------------
-- push
------------------------------------------------------------------------------

create or replace function wiki.push(
  p_message text    default null,
  p_paths   ltree[] default null
)
returns setof wiki.push_result
language plpgsql volatile security invoker
set search_path = wiki, public, pg_temp as $$
declare
  v_user     uuid := wiki.current_user_id();
  v_results  wiki.push_result[] := '{}';
  v_ok       boolean := true;
  v_vacated  ltree[]  := '{}';

  d          wiki.draft;
  v_doc      wiki.document;
  v_live     wiki.document_version;
  v_parent   wiki.document;
  v_target   uuid;
  v_result   wiki.push_result;
  v_version  integer;
begin
  if v_user is null then
    raise exception 'push requires an authenticated caller'
      using errcode = 'insufficient_privilege';
  end if;

  -- Paths freed by moves in this same changeset count as available, so
  -- "rename A to B, then create a new A" works in one push.
  select coalesce(array_agg(doc.path), '{}') into v_vacated
    from wiki.draft dr
    join wiki.document doc on doc.id = dr.document_id
   where dr.author_id = v_user
     and dr.operation = 'move'
     and (p_paths is null or dr.path = any(p_paths));

  --------------------------------------------------------------------------
  -- Pass 1: validate the whole changeset, writing nothing.
  --------------------------------------------------------------------------
  for d in
    select * from wiki.draft
     where author_id = v_user
       and (p_paths is null or path = any(p_paths))
     order by nlevel(path), path
  loop
    v_result := (d.path, d.operation, 'published',
                 null, null, null, null, null, null)::wiki.push_result;
    v_doc    := null;
    v_live   := null;

    -- Checked before anything else, and checked here rather than left to the
    -- client. A half-resolved merge published is worse than a conflict: the
    -- markers become the document and every later reader inherits them. The
    -- client decides when a merge is finished; the server refuses to take its
    -- word for it while the draft still says otherwise.
    if d.state = 'conflicted' then
      v_result.status := 'unmerged';
      v_result.detail := 'the merge is unresolved; finish it or back it out';
    end if;

    if v_result.status = 'published' and d.operation <> 'create' then
      select * into v_doc from wiki.document where id = d.document_id;

      if v_doc.id is null then
        v_result.status := 'missing';
        v_result.detail := 'the document no longer exists';
      elsif v_doc.is_folder then
        -- Folders carry no revisions, so there is nothing to supersede. Folder
        -- restructuring is an 'administer' operation and is not modelled here.
        v_result.status := 'invalid';
        v_result.detail := 'folders cannot be published through push';
      else
        select * into v_live
          from wiki.document_version
         where document_id = v_doc.id and upper_inf(valid);

        if v_live.id is null then
          v_result.status := 'invalid';
          v_result.detail := 'the document has no live revision';
        elsif v_live.version is distinct from d.base_version then
          v_result.status         := 'conflict';
          v_result.server_version := v_live.version;
          v_result.server_hash    := v_live.content_hash;
          v_result.server_content := v_live.content;

          -- The ancestor the draft descends from. It is a closed revision, so
          -- it is found by version rather than by upper_inf(), and it can
          -- legitimately be absent: base_version is null for a create, and a
          -- draft can outlive the revision it named if history was purged.
          select dv.content into v_result.base_content
            from wiki.document_version dv
           where dv.document_id = v_doc.id
             and dv.version = d.base_version;

          v_result.detail := format(
            'edited from revision %s but the server is at %s',
            d.base_version, v_live.version);
        end if;
      end if;
    end if;

    -- Per-operation checks, only if nothing has gone wrong yet.
    if v_result.status = 'published' then
      case d.operation

        when 'create' then
          if nlevel(d.path) < 2 then
            v_result.status := 'invalid';
            v_result.detail := 'cannot create a document at the root';
          elsif exists (select 1 from wiki.document where path = d.path)
                and not (d.path = any(v_vacated)) then
            select dv.version, dv.content_hash, dv.content
              into v_result.server_version, v_result.server_hash, v_result.server_content
              from wiki.document doc
              join wiki.document_version dv
                on dv.document_id = doc.id and upper_inf(dv.valid)
             where doc.path = d.path;
            v_result.status := 'conflict';
            -- A retired document still occupies its path, on purpose: history
            -- follows the path. Reinstating is an update on top of the
            -- tombstone, not a fresh create.
            v_result.detail := 'a document already exists at this path';
          elsif not wiki.has_capability(
                  wiki.nearest_existing_ancestor(d.path), 'create', v_user) then
            v_result.status := 'forbidden';
            v_result.detail := 'no create capability on the containing folder';
          elsif not wiki.can(d.path, false, v_user, 'create', v_user) then
            -- Create on the folder is not create at *this* path: an inherited
            -- allow can be overridden by a deny on the name itself. Without
            -- this the insert goes ahead and RLS rejects it, which aborts the
            -- whole call with a bare 42501 instead of reporting one refused
            -- document — the caller loses the report for everything else in
            -- the changeset along with it.
            v_result.status := 'forbidden';
            v_result.detail := 'the ACL denies creating at this path';
          end if;

        when 'update' then
          if not wiki.has_capability(v_doc.id, 'write', v_user) then
            v_result.status := 'forbidden';
            v_result.detail := 'no write capability on this document';
          end if;

        when 'delete' then
          if v_live.is_tombstone then
            v_result.status := 'invalid';
            v_result.detail := 'the document is already retired';
          elsif not wiki.has_capability(v_doc.id, 'delete', v_user) then
            v_result.status := 'forbidden';
            v_result.detail := 'no delete capability on this document';
          end if;

        when 'move' then
          -- A move needs write on what is moving and create where it lands.
          select * into v_parent
            from wiki.document
           where path = subpath(d.path, 0, nlevel(d.path) - 1);

          if nlevel(d.path) < 2 then
            v_result.status := 'invalid';
            v_result.detail := 'cannot move a document to the root';
          elsif v_parent.id is null then
            v_result.status := 'missing';
            v_result.detail := 'the destination folder does not exist';
          elsif not v_parent.is_folder then
            v_result.status := 'invalid';
            v_result.detail := 'the destination is not a folder';
          elsif exists (select 1 from wiki.document where path = d.path)
                and not (d.path = any(v_vacated)) then
            v_result.status := 'conflict';
            v_result.detail := 'a document already exists at the destination';
          elsif not wiki.has_capability(v_doc.id, 'write', v_user) then
            v_result.status := 'forbidden';
            v_result.detail := 'no write capability on this document';
          elsif not wiki.has_capability(v_parent.id, 'create', v_user) then
            v_result.status := 'forbidden';
            v_result.detail := 'no create capability on the destination folder';
          end if;

      end case;
    end if;

    if v_result.status <> 'published' then
      v_ok := false;
    end if;

    v_results := v_results || v_result;
  end loop;

  if not v_ok then
    return query select * from unnest(v_results);
    return;
  end if;

  --------------------------------------------------------------------------
  -- Pass 2: apply. Moves first so they free their old paths for later creates.
  --------------------------------------------------------------------------
  for d in
    select * from wiki.draft
     where author_id = v_user
       and (p_paths is null or path = any(p_paths))
     order by array_position(array['move', 'delete', 'update', 'create']::text[],
                             operation::text),
              nlevel(path), path
  loop
    case d.operation

      when 'create' then
        v_target := wiki.ensure_folder(subpath(d.path, 0, nlevel(d.path) - 1));

        insert into wiki.document (parent_id, slug, is_folder, title, owner_id, created_by)
        values (v_target, subpath(d.path, nlevel(d.path) - 1, 1)::text, false,
                subpath(d.path, nlevel(d.path) - 1, 1)::text, v_user, v_user)
        returning id into v_target;

        v_version := wiki.publish_revision(
          v_target, null, d.path, d.content, d.content_type,
          coalesce(d.message, p_message), false);

      when 'update' then
        v_version := wiki.publish_revision(
          d.document_id, d.base_version, d.path, d.content, d.content_type,
          coalesce(d.message, p_message), false);

      when 'delete' then
        v_version := wiki.publish_revision(
          d.document_id, d.base_version, d.path, null, 'text/markdown',
          coalesce(d.message, p_message), true);

      when 'move' then
        select * into v_doc from wiki.document where id = d.document_id;
        select * into v_live
          from wiki.document_version
         where document_id = d.document_id and upper_inf(valid);

        update wiki.document
           set parent_id = (select id from wiki.document
                             where path = subpath(d.path, 0, nlevel(d.path) - 1)),
               slug      = subpath(d.path, nlevel(d.path) - 1, 1)::text
         where id = d.document_id;

        -- The rename is itself a revision, so history records where the
        -- document lived and when.
        v_version := wiki.publish_revision(
          d.document_id, d.base_version, d.path,
          coalesce(d.content, v_live.content), coalesce(d.content_type, v_live.content_type),
          coalesce(d.message, p_message), false);

    end case;

    -- Report against the path the client asked for, which is how it will find
    -- the row in its own draft list.
    v_results := array(
      select case when r.path = d.path and r.operation = d.operation
                  then (r.path, r.operation, r.status, v_version,
                        r.server_version, r.server_hash, r.server_content,
                        r.base_content, r.detail)::wiki.push_result
                  else r end
        from unnest(v_results) r);
  end loop;

  delete from wiki.draft
   where author_id = v_user
     and (p_paths is null or path = any(p_paths));

  return query select * from unnest(v_results);
end;
$$;

comment on function wiki.push(text, ltree[]) is
  'Publish the caller''s drafts atomically. All or nothing: if any returned row '
  'has a status other than ''published'', nothing was written and the drafts '
  'remain. SECURITY INVOKER: every statement inside is subject to RLS, and the '
  'explicit capability checks are a second layer that turns a refusal into a '
  'reported row instead of an aborted transaction.';

------------------------------------------------------------------------------
-- Getting into and out of a merge
------------------------------------------------------------------------------

-- Record that a merge rewrote a draft.
--
-- The merge itself is client work — the server has no opinion about what
-- someone meant — but the *bookkeeping* belongs here, so that backing out is a
-- single statement rather than a client remembering to keep a copy of
-- something. p_conflicted is the client's verdict on its own merge.
--
-- Idempotent in the way that matters: merging twice does not lose the original,
-- because pre_merge_content is only filled if it is empty. A user who merges,
-- resolves badly, merges again and then backs out still lands on the text they
-- wrote before any of it.
create or replace function wiki.begin_merge(
  p_path        ltree,
  p_content     text,
  p_merged_from integer,
  p_conflicted  boolean
)
returns setof wiki.draft
language sql volatile
set search_path = wiki, public, pg_temp as $$
  update wiki.draft
     set pre_merge_content = coalesce(pre_merge_content, content),
         content           = p_content,
         merged_from       = p_merged_from,
         state             = case when p_conflicted then 'conflicted'
                                  else state end,
         updated_at        = now()
   where author_id = wiki.current_user_id()
     and path = p_path
  returning *;
$$;

-- The user finished resolving. Rebase onto the revision the merge pulled in and
-- forget the merge, keeping the text they settled on.
create or replace function wiki.resolve_merge(p_path ltree)
returns setof wiki.draft
language sql volatile
set search_path = wiki, public, pg_temp as $$
  update wiki.draft
     set base_version      = coalesce(merged_from, base_version),
         state             = 'clean',
         pre_merge_content = null,
         merged_from       = null,
         updated_at        = now()
   where author_id = wiki.current_user_id()
     and path = p_path
  returning *;
$$;

-- Back out. The pre-conflict text returns and the draft is exactly what it was
-- before anyone merged anything — which is the promise that makes merging safe
-- to offer at all. Published history was never involved.
create or replace function wiki.abort_merge(p_path ltree)
returns setof wiki.draft
language sql volatile
set search_path = wiki, public, pg_temp as $$
  update wiki.draft
     set content           = coalesce(pre_merge_content, content),
         state             = 'clean',
         pre_merge_content = null,
         merged_from       = null,
         updated_at        = now()
   where author_id = wiki.current_user_id()
     and path = p_path
  returning *;
$$;

grant execute on function
    wiki.push(text, ltree[]),
    wiki.publish_revision(uuid, integer, ltree, text, text, text, boolean),
    wiki.ensure_folder(ltree),
    wiki.nearest_existing_ancestor(ltree),
    wiki.begin_merge(ltree, text, integer, boolean),
    wiki.resolve_merge(ltree),
    wiki.abort_merge(ltree)
  to fswiki_user;
