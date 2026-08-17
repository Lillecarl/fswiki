-- Test fixtures. Loaded as the table owner, which bypasses RLS.
--
--   users                     groups
--     alice (superuser)         everyone     bob carol dave frank grace
--     bob                       engineering  bob carol frank
--     carol                     contractors  carol
--     dave                      auditors     grace
--     erin  (no groups at all)
--     frank
--     grace
--
--   tree                          ACEs
--     root
--     +- public                   everyone    allow reader   CI OI
--     |                           auditors    allow editor   CI OI NP
--     |  +- welcome
--     |  +- archive
--     |     +- old-post
--     +- engineering              engineering allow editor   CI OI
--     |                           contractors DENY  reader   CI OI
--     |  +- onboarding            carol       allow reader   (explicit)
--     |  +- secret-plans
--     |  +- private   [inheritance blocked]
--     |                           everyone    allow reader   CI OI
--     |     +- memo
--     +- locked   (owned by dave) everyone    DENY  owner    CI OI
--     +- io-test                  everyone    allow reader   CI OI IO
--        +- child
--
-- The interesting cases:
--   * carol is denied read across engineering by an inherited contractors ACE,
--     but an explicit allow on `onboarding` outranks it — the thing per-object
--     ACLs do that a scoped grant cannot.
--   * `private` blocks inheritance, so bob's engineering editor rights stop at
--     its boundary and only its own ACE applies.
--   * dave owns `locked` and is denied everything on it, yet keeps 'grant' so
--     he can dig himself out.

insert into wiki.principal (kind, name) values
  ('user',  'alice'), ('user', 'bob'),   ('user', 'carol'),
  ('user',  'dave'),  ('user', 'erin'),  ('user', 'frank'), ('user', 'grace'),
  ('group', 'everyone'), ('group', 'engineering'),
  ('group', 'contractors'), ('group', 'auditors');

insert into wiki.user_account (principal_id, oidc_issuer, oidc_subject, email, display_name, is_superuser)
select p.id, 'https://idp.test', p.name, p.name || '@test.invalid', initcap(p.name),
       (p.name = 'alice')
  from wiki.principal p
 where p.kind = 'user';

insert into wiki.group_member (group_id, member_id)
select g.id, m.id
  from (values
      ('everyone',    'bob'),   ('everyone',    'carol'), ('everyone', 'dave'),
      ('everyone',    'frank'), ('everyone',    'grace'),
      ('engineering', 'bob'),   ('engineering', 'carol'), ('engineering', 'frank'),
      ('contractors', 'carol'),
      ('auditors',    'grace')
    ) as e(group_name, member_name)
  join wiki.principal g on g.kind = 'group' and g.name = e.group_name
  join wiki.principal m on m.kind = 'user'  and m.name = e.member_name;

update wiki.document
   set owner_id = (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
 where path = 'root';

------------------------------------------------------------------------------
-- Tree
------------------------------------------------------------------------------

create or replace function pg_temp.mkdoc(
  p_parent text, p_slug text, p_folder boolean, p_title text, p_owner text default 'alice'
) returns void language sql as $$
  insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
  select d.id, p_slug, p_folder, p_title,
         (select p.id from wiki.principal p where p.kind = 'user' and p.name = p_owner)
    from wiki.document d where d.path = p_parent::ltree;
$$;

select pg_temp.mkdoc('root',                    'public',       true,  'Public');
select pg_temp.mkdoc('root.public',             'welcome',      false, 'Welcome');
select pg_temp.mkdoc('root.public',             'archive',      true,  'Archive');
select pg_temp.mkdoc('root.public.archive',     'old-post',     false, 'Old Post');
select pg_temp.mkdoc('root',                    'engineering',  true,  'Engineering');
select pg_temp.mkdoc('root.engineering',        'onboarding',   false, 'Onboarding');
select pg_temp.mkdoc('root.engineering',        'secret-plans', false, 'Secret Plans');
select pg_temp.mkdoc('root.engineering',        'private',      true,  'Private');
select pg_temp.mkdoc('root.engineering.private','memo',         false, 'Memo');
select pg_temp.mkdoc('root',                    'locked',       false, 'Locked', 'dave');
select pg_temp.mkdoc('root',                    'io-test',      true,  'Inherit Only');
select pg_temp.mkdoc('root.io-test',            'child',        false, 'Child');

update wiki.document set inheritance_blocked = true where path = 'root.engineering.private';

-- One published revision per leaf. `valid` defaults to an open interval, so
-- each is immediately the live one.
insert into wiki.document_version (document_id, version, path, content, message, author_id)
select d.id, 1, d.path, 'Contents of ' || d.title, 'initial',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d
 where not d.is_folder;

------------------------------------------------------------------------------
-- ACEs
------------------------------------------------------------------------------

create or replace function pg_temp.mkace(
  p_path text, p_principal text, p_role text, p_type text,
  p_ci boolean default true, p_oi boolean default true,
  p_io boolean default false, p_np boolean default false
) returns void language sql as $$
  insert into wiki.ace (document_id, principal_id, role_id, ace_type,
                        container_inherit, object_inherit, inherit_only, no_propagate)
  select d.id, p.id, r.id, p_type::wiki.ace_type, p_ci, p_oi, p_io, p_np
    from wiki.document d, wiki.principal p
   cross join lateral (select wiki.role_id(p_role) as id) r
   where d.path = p_path::ltree and p.name = p_principal;
$$;

select pg_temp.mkace('root.public',              'everyone',    'reader', 'allow');
select pg_temp.mkace('root.public',              'auditors',    'editor', 'allow',
                     p_np => true);
select pg_temp.mkace('root.engineering',         'engineering', 'editor', 'allow');
select pg_temp.mkace('root.engineering',         'contractors', 'reader', 'deny');
select pg_temp.mkace('root.engineering.onboarding', 'carol',    'reader', 'allow');
select pg_temp.mkace('root.engineering.private',  'everyone',   'reader', 'allow');
select pg_temp.mkace('root.locked',               'everyone',   'owner',  'deny');
-- Readable in the browser, but never mirrored to a laptop.
select pg_temp.mkace('root.engineering.secret-plans', 'everyone', 'sync', 'deny');
select pg_temp.mkace('root.io-test',              'everyone',   'reader', 'allow',
                     p_io => true);

-- A custom role holding `delete` and nothing else, to check that retiring a
-- document does not drag `write` in with it. Frank gets it on `io-test`, where
-- the inherit-only ACE above gives him no access of his own.
insert into wiki.role (name, description)
values ('retirer', 'Retire documents without being able to edit them');
insert into wiki.role_capability (role_id, capability)
values (wiki.role_id('retirer'), 'delete');

select pg_temp.mkace('root.io-test', 'frank', 'retirer', 'allow');
