-- 020_rbac.sql, the seed half.
-- The file header, and the reasoning, are in ../tables/020_rbac.sql.

insert into wiki.capability_requires (capability, requires) values
  ('write',      'read'),
  ('create',     'read'),
  ('delete',     'read'),
  ('grant',      'read'),
  ('administer', 'grant'),
  ('purge',      'delete'),
  ('purge',      'administer'),
  -- `sync` requires read but nothing requires `sync`: denying it leaves a
  -- document readable in the browser while keeping it off local disks. That is
  -- the audit-trail lever — every view is a request the server sees, instead of
  -- one bulk copy followed by silence.
  ('sync',       'read')
-- seed/ is replayed on every start, so every statement in it has to be
-- idempotent. This one is the requirement DAG, which is a fixed fact about the
-- capability model rather than anything a deployment edits.
on conflict (capability, requires) do nothing;
