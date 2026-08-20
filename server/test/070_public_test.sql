-- The `public` group: pages readable without an account.
--
-- Two layers have to agree before an anonymous browser sees a page, and this
-- file tests both. The ACL layer decides who a caller resolves to and which
-- rows RLS then admits. The grant layer decides what the fswiki_anon database
-- role may touch at all, and it is the one that matters most here, because a
-- wiki holding company secrets is one careless GRANT away from publishing
-- them.
--
-- The first blocks drop to fswiki_user with the token cleared, which isolates
-- the ACL from the grants: it answers "who does a caller with no account
-- resolve to" without the grant layer refusing first. Everything from
-- "The surface" onward runs as fswiki_anon itself, which is what an
-- unauthenticated PostgREST request actually arrives as.

------------------------------------------------------------------------------
-- The group itself
------------------------------------------------------------------------------

select wiki_test.expect_eq('public is a built-in group',
  (select count(*)::int from wiki.principal
    where kind = 'group' and name = 'public'), 1);

-- Nobody is a member, and nothing should ever add one: membership is what
-- effective_principals() supplies, not what group_member holds.
select wiki_test.expect_eq('public has no members',
  (select count(*)::int from wiki.group_member gm
     join wiki.principal p on p.id = gm.group_id
    where p.kind = 'group' and p.name = 'public'), 0);

------------------------------------------------------------------------------
-- A page granted to public
------------------------------------------------------------------------------

insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
select d.id, 'notices', false, 'Notices',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d where d.path = 'root'::ltree;

insert into wiki.document_version (document_id, version, path, content, message, author_id)
select d.id, 1, d.path, 'Contents of ' || d.title, 'initial',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d where d.path = 'root.notices'::ltree;

insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id,
       (select p.id from wiki.principal p where p.kind = 'group' and p.name = 'public'),
       (select r.id from wiki.role r where r.name = 'reader'),
       'allow'
  from wiki.document d where d.path = 'root.notices'::ltree;

------------------------------------------------------------------------------
-- Everyone is in public, logged in or not
------------------------------------------------------------------------------

-- erin has no groups and no ACEs. 020_rls_test.sql asserts she sees literally
-- nothing; the only thing that has changed is the grant to public, so anything
-- she can see now, she can see *because* of it.
select wiki_test.login('erin');
set role fswiki_user;

select wiki_test.expect_eq('erin: still resolves to a real principal',
  (select wiki.current_user_id() is not null), true);
select wiki_test.expect_eq('erin: sees the public page and the route to it',
  (select array_agg(path::text order by path) from wiki.document),
  array['root', 'root.notices']);
select wiki_test.expect_eq('erin: may read its content',
  (select count(*)::int from wiki.document_version), 1);

reset role;

-- And a user who already had access keeps it, with public on top rather than
-- instead: bob reads his own tree as well as the public page.
select wiki_test.login('bob');
set role fswiki_user;

select wiki_test.expect_eq('bob: public is added to what he already had',
  (select count(*)::int from wiki.document where path = 'root.notices'::ltree), 1);
select wiki_test.expect_eq('bob: and he still sees more than erin does',
  (select count(*)::int > 2 from wiki.document), true);

reset role;

------------------------------------------------------------------------------
-- No account at all
------------------------------------------------------------------------------

select set_config('request.jwt.claims', '', false);

select wiki_test.expect_eq('anonymous: resolves to no user',
  (select wiki.current_user_id() is null), true);
select wiki_test.expect_eq('anonymous: resolves to public and nothing else',
  (select array_agg(ep.principal_id) from wiki.effective_principals(null) ep),
  array[(select p.id from wiki.principal p
          where p.kind = 'group' and p.name = 'public')]);

set role fswiki_user;

select wiki_test.expect_eq('anonymous: sees the public page',
  (select array_agg(path::text order by path) from wiki.document),
  array['root', 'root.notices']);

-- The reason this is a group and not an anonymous user account. These policies
-- are phrased `current_user_id() is not null`, and a user account would have
-- turned every one of them on for the whole internet at once.
select wiki_test.expect_eq('anonymous: sees no principals',
  (select count(*)::int from wiki.principal), 0);
select wiki_test.expect_eq('anonymous: sees no user accounts',
  (select count(*)::int from wiki.user_account), 0);
select wiki_test.expect_eq('anonymous: sees no group memberships',
  (select count(*)::int from wiki.group_member), 0);
select wiki_test.expect_eq('anonymous: sees no drafts',
  (select count(*)::int from wiki.draft), 0);

reset role;
------------------------------------------------------------------------------
-- The surface: everything fswiki_anon can reach, named exactly
------------------------------------------------------------------------------
--
-- These are allow-list assertions rather than spot checks on purpose. A test
-- that says "anon cannot read wiki.principal" passes forever and says nothing
-- about the table added next year; a test that says "anon can read exactly
-- these four relations" fails the moment anything is added, which is the point.

select wiki_test.expect_eq('anon may select exactly the read path',
  (select coalesce(array_agg(c.relname::text order by c.relname), '{}')
     from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'wiki' and c.relkind in ('r', 'v')
      and has_table_privilege('fswiki_anon', c.oid, 'select')),
  array[-- The bytes of an attachment, under a policy that is document_version's
        -- with a different table name: `read` on the document it is. A public
        -- page with a picture on it is a public page.
        'attachment',
        'current_document', 'document', 'document_version',
        'syncable_document']);

select wiki_test.expect_eq('anon may not write anything, anywhere',
  (select coalesce(array_agg(c.relname::text order by c.relname), '{}')
     from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'wiki' and c.relkind in ('r', 'v')
      and (has_table_privilege('fswiki_anon', c.oid, 'insert')
        or has_table_privilege('fswiki_anon', c.oid, 'update')
        or has_table_privilege('fswiki_anon', c.oid, 'delete'))),
  '{}'::text[]);

select wiki_test.expect_eq('anon may execute exactly the self-only forms',
  (select coalesce(array_agg(
            p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
            order by p.proname, pg_get_function_identity_arguments(p.oid)), '{}')
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'wiki'
      and has_function_privilege('fswiki_anon', p.oid, 'execute')),
  array[-- document_select's own three, added when the policy stopped walking
        -- the tree once per row. acl_context() is the self-only overload, so
        -- there is no principal to name; the form that takes one stays
        -- revoked, and the assertion below is what holds that.
        --
        -- What acl_context() hands back is deliberately not the ACL. Every
        -- executable function is a PostgREST RPC, so a caller can ask for
        -- whatever a policy can: paths in there would be the list of pages
        -- hidden from the asker. It carries sha256 of each path instead --
        -- enough to confirm a path already guessed, which wiki.can() answers
        -- anyway, and not enough to enumerate one. See tables/120.
        'acl_context(p_cap wiki.capability)',
        -- current_document exposes `capabilities`, which is one question per
        -- capability per row. Same shape, same self-only rule.
        'acl_contexts()',
        -- One attachment's bytes by path. SECURITY INVOKER over both tables,
        -- so a file anon may not read is a file that is not there -- and it
        -- takes a path rather than a principal.
        'attachment_at(p_path ltree)',
        'can(p_path ltree, p_is_folder boolean, p_owner uuid, p_cap wiki.capability)',
        -- Reads no table. It answers from the context it is handed, so a
        -- caller who invents one learns only what their invention says.
        'can_ctx(p_path ltree, p_is_folder boolean, p_owner uuid, p_cap wiki.capability, p_ctx wiki.acl_context)',
        'can_traverse(p_document uuid, p_cap wiki.capability)',
        'can_traverse(p_path ltree, p_cap wiki.capability)',
        'capabilities_at(p_document uuid)',
        'capabilities_at_ctx(p_path ltree, p_is_folder boolean, p_owner uuid, p_ctxs wiki.acl_context[])',
        'has_capability(p_document uuid, p_cap wiki.capability)',
        -- The upload cap. Granted so a refusal can name the number; anon
        -- cannot upload anything, and the row it reads is granted to nobody.
        'max_attachment_bytes()',
        -- sha256 of the argument. can_ctx() calls it and is not SECURITY
        -- DEFINER, so it needs the grant; it discloses nothing that the
        -- caller did not supply.
        'path_key(p_path ltree)',
        -- PostgREST runs db-pre-request on every request, anonymous ones
        -- included, so this one is not optional. It takes no arguments and
        -- reads only the request headers; the block below is what stops those
        -- headers being worth anything without an account.
        'pre_request()',
        -- Full-text search. SECURITY DEFINER, so it reads content as the
        -- owner and then applies `read` by hand -- see runtime/078_search.sql
        -- for why, and 100_search_test.sql for the cross-product comparison
        -- that holds the hand-applied copy to the policy it copies. Takes no
        -- principal, so an anonymous caller can only ever ask what `public`
        -- may find. `search_drafts` is deliberately not here.
        'search(p_query text, p_limit integer)',
        -- The browser read. Gated on `read` through current_document, takes
        -- no principal, and writes no audit row for a caller with no account.
        'view_document(p_document uuid, p_event jsonb)']);

-- The invariant behind that list, stated so it survives the list changing.
-- Every ACL function that takes a principal is an oracle in the hands of
-- someone with no account: it answers, as the owner and with RLS bypassed,
-- what a named stranger may read. None of them may ever be granted here.
select wiki_test.expect_eq('no function anon may execute takes a principal',
  (select coalesce(array_agg(p.proname::text order by p.proname), '{}')
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'wiki'
      and has_function_privilege('fswiki_anon', p.oid, 'execute')
      and 'p_user' = any(coalesce(p.proargnames, '{}'::text[]))),
  '{}'::text[]);

-- Named individually as well, because these are the specific doors the
-- lockdown was written to shut and a reader of this file should see them.
select wiki_test.expect_eq('anon cannot ask what someone else may do',
  has_function_privilege('fswiki_anon', 'wiki.capabilities_at(uuid, uuid)', 'execute'),
  false);
select wiki_test.expect_eq('anon cannot ask why someone else may do it',
  has_function_privilege('fswiki_anon', 'wiki.explain_acl(uuid, uuid)', 'execute'),
  false);
select wiki_test.expect_eq('anon cannot walk the ACL for a named principal',
  has_function_privilege('fswiki_anon',
    'wiki.can(ltree, boolean, uuid, wiki.capability, uuid)', 'execute'),
  false);
select wiki_test.expect_eq('anon cannot test a capability for a named principal',
  has_function_privilege('fswiki_anon',
    'wiki.has_capability(uuid, wiki.capability, uuid)', 'execute'),
  false);
select wiki_test.expect_eq('anon cannot resolve identity',
  has_function_privilege('fswiki_anon', 'wiki.current_user_id()', 'execute'), false);
select wiki_test.expect_eq('anon cannot poll for write activity',
  has_function_privilege('fswiki_anon', 'wiki.change_token()', 'execute'), false);
select wiki_test.expect_eq('anon cannot publish',
  has_function_privilege('fswiki_anon', 'wiki.push(text, ltree[])', 'execute'), false);
select wiki_test.expect_eq('anon cannot take a borrowed identity',
  has_function_privilege('fswiki_anon',
    'wiki.begin_impersonation(uuid, uuid[], text, text)', 'execute'), false);

------------------------------------------------------------------------------
-- What an unauthenticated request actually sees
------------------------------------------------------------------------------

select set_config('request.jwt.claims', '', false);
set role fswiki_anon;

select wiki_test.expect_eq('anon: sees the public page and the route to it',
  (select array_agg(path::text order by path) from wiki.document),
  array['root', 'root.notices']);

-- Every other fixture document is granted to a user or a real group, and none
-- of them may leak through `public`.
select wiki_test.expect_eq('anon: sees no engineering document',
  (select count(*)::int from wiki.document where path <@ 'root.engineering'::ltree), 0);
select wiki_test.expect_eq('anon: sees no content it was not granted',
  (select count(*)::int from wiki.document_version), 1);
select wiki_test.expect_eq('anon: reads the public page through the view',
  (select content from wiki.current_document where path = 'root.notices'::ltree),
  'Contents of Notices');
select wiki_test.expect_eq('anon: the syncable view agrees',
  (select count(*)::int from wiki.syncable_document
    where path = 'root.notices'::ltree), 1);

-- A document that is not readable and one that does not exist must be the same
-- answer. This is the property the whole project is built around, and an
-- unauthenticated caller is where it is easiest to get wrong.
select wiki_test.expect_eq('anon: a secret page is indistinguishable from a missing one',
  (select count(*)::int from wiki.document
    where path = 'root.engineering.secret-plans'::ltree),
  (select count(*)::int from wiki.document
    where path = 'root.no-such-page-anywhere'::ltree));

------------------------------------------------------------------------------
-- What it cannot do
------------------------------------------------------------------------------

select wiki_test.expect_denied('anon cannot list principals',
  'select count(*) from wiki.principal');
select wiki_test.expect_denied('anon cannot list user accounts',
  'select count(*) from wiki.user_account');
select wiki_test.expect_denied('anon cannot list group memberships',
  'select count(*) from wiki.group_member');
select wiki_test.expect_denied('anon cannot read the ACL',
  'select count(*) from wiki.ace');
select wiki_test.expect_denied('anon cannot read drafts',
  'select count(*) from wiki.draft');
select wiki_test.expect_denied('anon cannot read the access log',
  'select count(*) from wiki.access_event');
select wiki_test.expect_denied('anon cannot read the role definitions',
  'select count(*) from wiki.role');
select wiki_test.expect_denied('anon cannot create a document',
  $q$insert into wiki.document (parent_id, slug, is_folder, title)
     select id, 'intruder', false, 'Intruder' from wiki.document
      where path = 'root'::ltree$q$);
select wiki_test.expect_denied('anon cannot edit the public page',
  $q$update wiki.document set title = 'Owned' where path = 'root.notices'::ltree$q$);
select wiki_test.expect_denied('anon cannot delete the public page',
  $q$delete from wiki.document where path = 'root.notices'::ltree$q$);
select wiki_test.expect_denied('anon cannot grant itself anything',
  $q$insert into wiki.ace (document_id, principal_id, role_id, ace_type)
     select d.id, p.id, r.id, 'allow'
       from wiki.document d, wiki.principal p, wiki.role r
      where d.path = 'root'::ltree and p.name = 'public' and r.name = 'owner'$q$);
select wiki_test.expect_denied('anon cannot save a draft',
  $q$insert into wiki.draft (author_id, operation, path)
     select id, 'update', 'root.notices'::ltree from wiki.principal
      where name = 'public'$q$);

reset role;

------------------------------------------------------------------------------
-- Taking it away again
------------------------------------------------------------------------------
--
-- A grant to public has to be revocable by the ordinary rules, or it is a
-- second permission system wearing the ACL's clothes.

insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id,
       (select p.id from wiki.principal p where p.kind = 'group' and p.name = 'public'),
       (select r.id from wiki.role r where r.name = 'reader'),
       'deny'
  from wiki.document d where d.path = 'root.notices'::ltree;

set role fswiki_anon;
select wiki_test.expect_eq('anon: a deny ACE on public takes the page back',
  (select count(*)::int from wiki.document where path = 'root.notices'::ltree), 0);
reset role;

delete from wiki.ace a
 using wiki.document d, wiki.principal p
 where a.document_id = d.id and d.path = 'root.notices'::ltree
   and a.principal_id = p.id and p.name = 'public' and a.ace_type = 'deny';

set role fswiki_anon;
select wiki_test.expect_eq('anon: and removing it gives the page back',
  (select count(*)::int from wiki.document where path = 'root.notices'::ltree), 1);
reset role;

------------------------------------------------------------------------------
-- Readable in a browser, and not on a laptop
------------------------------------------------------------------------------
--
-- `sync` is the audit lever: deny it and a page stays readable while every
-- view costs a request the server sees, instead of one bulk mirror followed by
-- silence. It has to work for a caller with no account too -- that is the case
-- it was most obviously written for.

insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
select d.id, 'bulletin', false, 'Bulletin',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d where d.path = 'root'::ltree;

insert into wiki.document_version (document_id, version, path, content, message, author_id)
select d.id, 1, d.path, 'Contents of ' || d.title, 'initial',
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'alice')
  from wiki.document d where d.path = 'root.bulletin'::ltree;

insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id,
       (select p.id from wiki.principal p where p.kind = 'group' and p.name = 'public'),
       (select r.id from wiki.role r where r.name = 'reader'),
       'allow'
  from wiki.document d where d.path = 'root.bulletin'::ltree;

insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id,
       (select p.id from wiki.principal p where p.kind = 'group' and p.name = 'public'),
       (select r.id from wiki.role r where r.name = 'sync'),
       'deny'
  from wiki.document d where d.path = 'root.bulletin'::ltree;

set role fswiki_anon;
select wiki_test.expect_eq('anon: a deny-sync page is still readable',
  (select count(*)::int from wiki.current_document
    where path = 'root.bulletin'::ltree), 1);
select wiki_test.expect_eq('anon: and is absent from the syncable tree',
  (select count(*)::int from wiki.syncable_document
    where path = 'root.bulletin'::ltree), 0);

-- And the point of all of it: the browser read serves that page, which is what
-- an audit lever is for. The mirroring read is not even reachable from here.
select wiki_test.expect_eq('anon: view_document serves the deny-sync page',
  (select content from wiki.view_document(wiki_test.doc('root.bulletin'))),
  'Contents of Bulletin');
select wiki_test.expect_denied('anon cannot call the mirroring read',
  'select * from wiki.read_document(wiki_test.doc(''root.bulletin''))');
reset role;

-- The same split for someone with an account, on a fixture that has carried a
-- deny-sync ACE since long before any of this: everyone is denied `sync` on
-- secret-plans, and bob reads it anyway.
select wiki_test.login('bob');
set role fswiki_user;

select wiki_test.expect_eq('bob: may read secret-plans',
  wiki.has_capability(wiki_test.doc('root.engineering.secret-plans'), 'read'), true);
select wiki_test.expect_eq('bob: but may not sync it',
  wiki.has_capability(wiki_test.doc('root.engineering.secret-plans'), 'sync'), false);
select wiki_test.expect_eq('bob: the browser read serves it',
  (select count(*)::int from wiki.view_document(
     wiki_test.doc('root.engineering.secret-plans'))), 1);
select wiki_test.expect_eq('bob: the mirroring read does not',
  (select count(*)::int from wiki.read_document(
     wiki_test.doc('root.engineering.secret-plans'))), 0);

reset role;

------------------------------------------------------------------------------
-- The one function anon must hold, and why it is not a way in
------------------------------------------------------------------------------
--
-- wiki.pre_request() is PostgREST's db-pre-request hook and runs inside every
-- request's transaction before anything else, so fswiki_anon has to be able to
-- execute it. It is also the only door into impersonation, and it reads its
-- instructions from request headers -- which anyone can send. What stops an
-- unauthenticated caller borrowing an identity is wiki.begin_impersonation()
-- checking a grant against the *authenticated* user, and there isn't one.

select set_config('request.jwt.claims', '', false);
select set_config('request.headers',
  '{"fswiki-act-as": "alice"}', false);
select set_config('request.method', 'GET', false);
select set_config('request.path', '/document', false);

set role fswiki_anon;

select wiki_test.expect_eq('anon cannot borrow a person by asking',
  wiki_test.sqlstate_of('select wiki.pre_request()') is not null, true);
-- root, root.notices and root.bulletin: what public was granted, unchanged.
select wiki_test.expect_eq('anon: and is still nobody afterwards',
  (select count(*)::int from wiki.document), 3);

reset role;

select set_config('request.headers',
  '{"fswiki-act-as-groups": "engineering"}', false);

set role fswiki_anon;

select wiki_test.expect_eq('anon cannot borrow a membership either',
  wiki_test.sqlstate_of('select wiki.pre_request()') is not null, true);
select wiki_test.expect_eq('anon: still sees only what public was granted',
  (select array_agg(path::text order by path) from wiki.document),
  array['root', 'root.bulletin', 'root.notices']);

reset role;
select set_config('request.headers', '', false);

------------------------------------------------------------------------------
-- Nobody owns a document as public
------------------------------------------------------------------------------
--
-- wiki.can() hands a document's owner a standing `grant` right, matched against
-- the caller's effective principals -- so an owning group confers it on that
-- group. `public` is in everybody's, which would make an owning `public` mean
-- "anyone may re-ACL this page", anonymous callers included. Reachable from
-- `write` alone, because owner_id is an ordinary column and document_update
-- gates on 'write'.

select wiki_test.expect_rejected('public cannot be given a document at insert',
  $q$insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
     select d.id, 'handover', false, 'Handover',
            (select p.id from wiki.principal p
              where p.kind = 'group' and p.name = 'public')
       from wiki.document d where d.path = 'root'::ltree$q$);

select wiki_test.expect_rejected('and a page cannot be handed over later',
  $q$update wiki.document
        set owner_id = (select p.id from wiki.principal p
                         where p.kind = 'group' and p.name = 'public')
      where path = 'root.notices'::ltree$q$);

-- The route that made it worth a trigger rather than a note: `write` is all it
-- would have taken. Bob gets editor on the bulletin and nothing else -- granted
-- here rather than borrowed from the fixtures, because 030_push_test.sql moves
-- documents about and a test that depends on where they ended up is a test that
-- fails for the wrong reason.
insert into wiki.ace (document_id, principal_id, role_id, ace_type)
select d.id,
       (select p.id from wiki.principal p where p.kind = 'user' and p.name = 'bob'),
       (select r.id from wiki.role r where r.name = 'editor'),
       'allow'
  from wiki.document d where d.path = 'root.bulletin'::ltree;

-- Asked as superuser: wiki_test.doc() is the caller's view of the tree.
select wiki_test.expect_eq('bob: may edit the bulletin',
  wiki.has_capability(wiki_test.doc('root.bulletin'), 'write',
                      wiki_test.who('bob')), true);
select wiki_test.expect_eq('bob: and holds no grant right on it',
  wiki.has_capability(wiki_test.doc('root.bulletin'), 'grant',
                      wiki_test.who('bob')), false);

select wiki_test.login('bob');
set role fswiki_user;
select wiki_test.expect_rejected('bob: but cannot hand it to the internet',
  $q$update wiki.document
        set owner_id = (select p.id from wiki.principal p
                         where p.kind = 'group' and p.name = 'public')
      where path = 'root.bulletin'::ltree$q$);
reset role;

-- Group ownership in general is untouched: only the group everyone is in is
-- refused.
select wiki_test.expect_eq('an ordinary group may still own a document',
  wiki_test.sqlstate_of(
    $q$update wiki.document
          set owner_id = (select p.id from wiki.principal p
                           where p.kind = 'group' and p.name = 'engineering')
        where path = 'root.bulletin'::ltree$q$) is null,
  true);
