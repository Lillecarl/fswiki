-- 030_documents.sql, the runtime half.
-- The file header, and the reasoning, are in ../tables/030_documents.sql.

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
