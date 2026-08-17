# fswiki server schema

Postgres schema for the wiki: documents, versions, drafts, and an NTFS-style
ACL enforced by row-level security.

    ./test/run.sh          # spins up a throwaway cluster, loads everything, runs the tests
    PGBIN=/nix/store/...-postgresql-18.4/bin ./test/run.sh

Requires PostgreSQL 16 or newer — ltree labels only accept hyphens and non-ASCII
from 16 on, and slugs depend on that.

## Load order

| file | contents |
| --- | --- |
| `001_extensions.sql` | `ltree`, `pgcrypto`, the `wiki` schema |
| `010_principals.sql` | users, groups, nested membership |
| `020_rbac.sql` | capabilities, roles, role inheritance |
| `030_documents.sql` | documents, versions, drafts, path maintenance |
| `035_acl.sql` | access control entries |
| `040_authz.sql` | identity, capability closures, the ACL walk, `has_capability()` |
| `050_rls.sql` | the policies |
| `060_roles.sql` | database roles for PostgREST |
| `070_views.sql` | `current_document`, `syncable_document`, `document_as_of()` |
| `075_changes.sql` | `change_token()`, for cheap "has anything changed" polling |
| `080_push.sql` | `wiki.push()` and the publish primitives |
| `900_builtin_roles.sql` | built-in roles and the tree root |
| `950_lockdown.sql` | revokes the EXECUTE that PostgreSQL grants to PUBLIC |

`950_lockdown.sql` must stay last. PostgreSQL makes every new function
executable by PUBLIC and offers no way to create one that is not, so the revoke
has to run after the final `create function` in the load — a revoke in
`060_roles.sql` would silently miss everything defined after it.

## The permission model

An **ACE** is attached to one document, names one principal (user or group),
carries one **role**, and is either allow or deny. Roles are named bundles of
**capabilities** and compose by inheritance, so `editor` covers everything
`author` does.

### Capabilities are hierarchical

Capabilities form a requirement DAG (`wiki.capability_requires`), so there is no
write without read:

    read <-- sync
         <-- write
         <-- create
         <-- delete ------- purge
         <-- grant <-- administer

The closure runs in **both directions, differently**:

| | closure | effect |
| --- | --- | --- |
| allow X | downward — everything X requires | allowing `write` also allows `read` |
| deny X | upward — everything requiring X | denying `read` also denies `write` |

The upward direction is the contrapositive, and it is what stops a principal
ending up able to write a document they cannot read. Roles therefore declare
only what they *add*: `editor` says `write` and gets `read` from the lattice.

Two placements are deliberate:

- **`delete` sits low**, directly on `read`, because everything is versioned:
  deleting writes a tombstone and the content is still there. Letting a
  librarian retire a page they cannot edit is reasonable, so it does not require
  `write`.
- **`purge` sits alone at the top** and is in no inherited role — not even
  `owner`. It is the only irrecoverable operation in the system, and it is the
  capability the `document` DELETE policy tests.

### Inheritance

Documents inherit ACEs from their ancestors. The four NTFS inheritance flags are
implemented with the same names and meanings:

| flag | meaning |
| --- | --- |
| `container_inherit` (CI) | reaches descendant folders |
| `object_inherit` (OI) | reaches descendant documents |
| `inherit_only` (IO) | does *not* apply to the document it sits on |
| `no_propagate` (NP) | reaches immediate children only |

`document.inheritance_blocked` is Windows' "disable inheritance": ACEs from
above stop at that document and do not reach it or anything below it.

### Precedence

Applicable ACEs are consulted **nearest first, deny before allow at equal
distance**, and the first one that mentions the capability decides:

    explicit deny  >  explicit allow  >  parent deny  >  parent allow  >  ...

Two consequences worth internalising:

- **Deny is not absolute.** An explicit allow on a document beats a deny
  inherited from its folder. That is the point of per-object ACLs, and it is why
  `wiki.explain_acl()` exists — with this rule you cannot read a verdict off the
  ACL by eye.
- **A deny reaches further than its role names.** Because the capability lattice
  closes upward on deny, denying `reader` also denies `write`, `create` and
  everything else that needs `read`. Denying `sync` is the narrow case, since
  nothing requires it.

With no matching ACE at all, the answer is no. The ACL is a closed world.

### Escape hatches

- `user_account.is_superuser` bypasses the ACL entirely.
- `document.owner_id` always retains `grant`, whatever the ACL says, so a
  deny-everything ACE cannot permanently lock a document. The owner can also
  *see* the ACEs on such a document (`ace_select`), without which the escape
  would not actually work — Postgres applies SELECT policies to the rows a
  DELETE touches.

### Traversal

A folder is visible when you may read it **or** when it contains something you
may read, at any depth (`wiki.can_traverse()`). Without this the tree comes back
disconnected and the FUSE mount is unusable.

This deliberately leaks folder *names* along the route to a readable document.
If a folder's name is itself sensitive, nothing beneath it may be granted out.

### Sync

`sync` gates taking a local copy via FUSE or the CLI. It requires `read`, but
nothing requires it, so denying `sync` leaves a document perfectly readable in
the browser while keeping it off laptops — every view then costs a request the
server can log. It is an audit lever, not a confidentiality one: anyone who can
read a page can still copy the text out by hand.

`wiki.syncable_document` is the view a client mirrors. It runs the traversal rule
on `sync` rather than `read`, so a client's tree and a browser's tree differ
exactly where a deny-sync ACE sits.

## Versioning

Full snapshots in a temporal table. `document_version.version` is the
human-facing revision number; `valid tstzrange` is the wall-clock interval
during which that revision was published.

    revision 1  [t0, t1)
    revision 2  [t1, t2)
    revision 3  [t2,   )   <- live: upper_inf(valid)

There is no `current_version_id` pointer — a cached pointer is a second source
of truth that can drift. The live revision is the one with an open interval, and
two database constraints keep that true regardless of which client is writing:

- `EXCLUDE USING gist (document_id WITH =, valid WITH &&)` forbids two revisions
  of one document being live at overlapping times. This is what `btree_gist` is
  for: it lets one GiST index mix uuid equality with range overlap.
- a partial unique index on `(document_id) WHERE upper_inf(valid)` forbids two
  open-ended revisions, and doubles as the lookup index for "get current".

The payoff is point-in-time reads: the whole wiki as of any instant is one
predicate, `valid @> t`, across every document at once. See
`wiki.document_as_of(timestamptz)` — it is not `SECURITY DEFINER`, so a caller
sees old content only where they have present-day permission.

Publishing means closing one interval and opening the next in one transaction.
Clients may do that themselves — `fswiki_user` holds `INSERT` and `UPDATE
(valid)` on `document_version`, gated by ordinary policies — but the
`document_version_immutable` trigger narrows the update to closing a live
revision and nothing else. So history is append-only for everyone including the
table owner, and the worst a hand-rolled client can do is skip `wiki.push()`'s
conflict detection and clobber an edit.

Deleting is a tombstone revision (`is_tombstone`, content null). The document row
and its history stay; `current_document` drops it. Actual destruction is a row
DELETE gated on `purge`.

Snapshots rather than deltas, on purpose. Markdown is small, TOAST already
compresses it, and reconstructing content from a delta chain on every read is
the beginning of writing a version control system — the thing this design is
trying not to do. If storage ever bites, dedupe by `content_hash` into a blob
table before reaching for deltas. Note that content-addressed blobs are shared
across documents in different ACL domains, so such a table must never be
readable directly, only through `document_version`.

## Identity

`wiki.current_user_id()` reads `request.jwt.claims`, the GUC PostgREST sets
after verifying a token. **This is only safe for clients that cannot execute
arbitrary SQL** — any role that can run statements can also set that GUC. A
direct libpq client must be authenticated as its own database role instead;
`wiki.current_user_id()` is the single place to hook that in.

Group membership is deliberately relational rather than read from token claims,
so revoking a membership takes effect immediately instead of at token expiry.

## Conventions

**String literals.** Capabilities, ACE types and draft operations are enums, so
`'read'` is checked when the statement is parsed — a typo is an error, not a
silent miss. Role and principal *names* are rows, so they are unchecked strings
and `where name = 'editior'` quietly matches nothing. Use `wiki.role_id(name)`
and `wiki.principal_id(kind, name)`, which raise, rather than inline lookups.

**Function grants are load-bearing, not cosmetic.** RLS policy expressions are
evaluated with the *querying* role's privileges, so `fswiki_user` must hold
EXECUTE on every function its policies call. A missing grant does not filter
more tightly — the whole statement fails with `permission denied for function`.
The converse holds too: a function called only from *inside* a `SECURITY
DEFINER` function is checked against that function's owner and needs no grant.

**RLS filters, it does not raise.** A row the DELETE or UPDATE policy rejects is
simply not among the rows affected, and the statement reports success. Clients
must check the row count; treating "no error" as "it worked" will silently do
nothing.

## Publishing

`wiki.push(message, paths)` promotes the caller's drafts to published revisions.
It is `SECURITY INVOKER` — RLS applies to it exactly as to any other statement,
and the policies are what enforce access. The `has_capability()` calls in its
validation pass exist so the caller gets a clean `forbidden` row back instead of
an aborted transaction; they are reporting, not enforcement.

It returns one `wiki.push_result` per draft. **All or nothing**: if any row has a
status other than `published`, nothing was written and the drafts remain, with
the server's current revision and content attached so the client can merge. That
is a product decision — a commit should be atomic the way `svn commit` is — not
something the versioning model requires; each document's revision chain is
independent.

Operations: `create` (auto-creating any missing folders on the way, and making
the creator the owner), `update`, `delete` (a tombstone), and `move` (recorded as
a revision, so history shows where a document used to live).

`wiki.publish_revision()` is the underlying primitive and is exposed too. It
takes the base revision and refuses if the server has moved past it, so calling
it directly loses the batching and the conflict report but cannot silently
clobber an edit.

Retired documents keep their path on purpose — history follows the path — so
creating over one is a conflict. Reinstating is an `update` on top of the
tombstone, not a fresh `create`.

## Two traps worth knowing

**`INSERT ... RETURNING` applies the SELECT policy to the row it just made.**
A policy that resolves permissions by looking the row up by id cannot decide
this: these helpers are `STABLE`, so they read the snapshot as of statement
start, where the new row does not exist — the ACL comes back empty and the
insert is refused. PostgREST adds `RETURNING` to every insert by default, so
this is the normal path, not a corner case.

**The same trap catches `UPDATE` more quietly.** A `WITH CHECK` that re-reads the
row by id sees the *old* path, so re-parenting a document into a subtree the
caller has no rights over sails straight through.

Both are why `wiki.can(path, is_folder, owner_id, cap)` — not
`wiki.has_capability(id, cap)` — is what the policies on `wiki.document` use.
The id-keyed form is for everything else, where the row certainly exists.

## Known gaps

- Folders cannot be moved, renamed or retired through push; only documents can.
  Folder restructuring is an 'administer' operation with no implementation yet.
- Push validates against the pre-push state, with one exception: paths freed by
  moves in the same changeset count as available. Anything more entangled has to
  be pushed in two steps.
- `can_traverse()` evaluates the full ACL of every descendant, so it is
  O(subtree) per folder. Correct, not fast. Materialise a per-request visible
  path set if it shows up in a profile.
- Nothing enforces that a folder has no revisions, or that a non-folder has no
  children.
- No audit log. `sync` exists to push reads through the server, but the server
  does not yet record them.
