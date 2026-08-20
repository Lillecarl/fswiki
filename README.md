# fswiki

A wiki that is a filesystem, with permissions that are real.

```console
$ fswiki-mount ~/wiki &            # runs in the foreground; & or another terminal
$ vim ~/wiki/engineering/onboarding.md
$ fswiki status
1 pending change:

   modified  engineering/onboarding  (from revision 2)

Publish with: fswiki push -m "..."

$ fswiki push -m "the laptop bit was wrong"
Published 1 change.

   published  engineering/onboarding  -> revision 3
```

Two things follow from that, and between them they are the whole project.

**The wiki is a directory.** Not "exports to markdown", not "has a git backend"
— a FUSE mount whose files are the documents. `vim` works. `rg` works. So does
anything else you already have, because there is nothing to integrate with.

**Permissions are enforced by the database, on every read.** An NTFS-style ACL
over an `ltree` path, evaluated by Postgres row-level security. There is no
application tier that could forget to check: the mount, the CLI, the renderer
and `psql` all get the same answer, because they all get it from the same
`USING` clause.

## What is unusual about it

**A page you may not read is indistinguishable from a page that is not there.**
Not merely hidden — *identical*. A link to it renders as the same plain text as
a link to nothing, `push` reports a create collision as `forbidden` rather than
handing back the occupant's content, and the mount simply does not list it.
The difference between "forbidden" and "missing" is itself a disclosure, and it
is the one an ACL was never asked to make. See
[docs/rendering.md](docs/rendering.md).

**Saving is not publishing.** A write through the mount becomes a *draft* — a
row that belongs to you, that nobody else sees, and that stays yours until you
push it. Commit mode, like SVN, because a wiki where every keystroke is live is
a wiki nobody drafts in. Push does a three-way merge against the revision you
actually read, so two people editing one page get a conflict rather than a
silent lost update.

**You can check an ACL by being someone else.**

```console
$ fswiki-mount --as bob ~/bobs-view
WARNING fswiki: mounting the view of bob — not yours. Read-only, and the
server has a record of it
$ ls ~/bobs-view/engineering/
onboarding.md  private/
```

An ACL is a prediction about what a person will be able to see, and `ls` is the
only honest way to check a prediction. It is read-only three times over — a
read-only transaction on the server, `ro` in the mount options so the kernel
refuses writes before they reach us, and `0444` so an editor says no first —
and the server logs the impersonation as a session before it will serve a byte
of it. You can also borrow a *membership* rather than a person, which answers
"what would a new engineer see" without inventing an employee. See
[docs/impersonation.md](docs/impersonation.md).

**Reads are audited by the request that serves them.** `POST /rpc/read_document`
returns the document and records the access in the same transaction, so there is
no window where one happened without the other — and a `GET` could not do it,
because PostgREST runs `GET` in a read-only transaction. Opens the mount serves
from its own cache go to a local queue that ships later, so the trail does not
develop holes that depend on what you read earlier. See
[docs/audit-trail.md](docs/audit-trail.md).

## The pieces

| | |
| --- | --- |
| [`server/`](server/) | The schema — documents, versions, drafts, the ACL, the RLS that enforces it — and the server that reads the wiki to a browser. PostgREST in front, no application tier. |
| [`core/`](core/) | Shared by both clients: the PostgREST client, path naming, the three-way merge, the render pipeline. |
| [`fuse/`](fuse/) | `fswiki-mount`. The tree, on **trio** — pyfuse3's native backend. |
| [`cli/`](cli/) | `fswiki`. status, diff, push, revert, merge, render, preview. |
| [`dev/`](dev/) | Real Postgres and PostgREST under process-compose. Nothing is mocked. |
| [`test/`](test/) | 961 tests against all of it, at 91% of the lines. See [test/README.md](test/README.md). |

## Running it

Everything is an attribute of the top-level `default.nix`, so one form runs all
of it. No flake, no `nix develop`, nothing to install:

| | |
| --- | --- |
| `nix run --file . dev` | Postgres + PostgREST under process-compose |
| `nix run --file . server` | the browser-facing reader, `fswiki-serve` |
| `nix run --file . cli -- <args>` | `fswiki` — status, diff, push, render, preview |
| `nix run --file . fuse -- ~/wiki` | `fswiki-mount` |
| `nix run --file . tests -- <pytest args>` | the suite |
| `nix build --file . tests.check -L` | the half of it that runs in a sandbox |
| `nix build --file . cli fuse server` | put them in `./result*` instead |

Everything after `--` goes to the program. `nix run` builds first, so the first
one is slow and the rest are not.

**Start the stack, then point a client at it.**

```console
$ nix run --file . dev                      # leave this running
```

In another shell:

```console
$ eval "$(nix run --file . dev -- env)"     # FSWIKI_URL, FSWIKI_TOKEN, PGPORT
$ export FSWIKI_TOKEN=$(nix run --file . dev -- token bob)

$ nix run --file . fuse -- ~/wiki
```

`nix run --file . dev -- reset` wipes `.dev/` and rebuilds the database from
scratch, which is how you reload the schema — the table half is not idempotent
by design. [`dev/README.md`](dev/README.md) has the ports, the fixture users and
what the seed builds.

The dev stack is Postgres on `127.0.0.1:55432` and PostgREST on `:3000`, loaded
with fixture users (`alice bob carol dave erin frank grace`) whose ACLs are
built to have interesting shapes rather than tidy ones. `fswiki-dev token bob`
signs a JWT with a local secret in exactly the form `wiki.current_user_id()`
resolves — no OIDC provider needed until there is one.

**Read the wiki in a browser**, without running any client at all. The server
is a separate program from the dev stack: it applies the schema itself and
starts a PostgREST of its own, so point it at the *database* rather than at the
dev stack's PostgREST.

```console
$ export FSWIKI_DATABASE_URL=postgres://postgres@127.0.0.1:55432/fswiki
$ export FSWIKI_JWT_SECRET=$(cat .dev/jwt-secret)   # so it accepts dev tokens
$ export FSWIKI_POSTGREST_PORT=3001                 # :3000 is the dev stack's
$ nix run --file . server
```

Then <http://127.0.0.1:8080>. [`server/README.md`](server/README.md) has the
rest of the environment, including `FSWIKI_RENDER_CACHE_BYTES`.

It loads the schema if the database is empty, starts a PostgREST of its own,
and serves. There is no login yet: a token goes in a `fswiki_session` cookie or
an `Authorization` header, and a visitor without one sees whatever has been
granted to the built-in `public` group — which is nothing until somebody grants
it something.

There is a browser view too, for while you are writing. Unlike the server it
shows **your drafts**, and it reloads itself:

```console
$ nix run --file . cli -- preview
fswiki preview on http://127.0.0.1:8222/
  read-only; ctrl-c to stop
```

It renders your drafts, not the published copy, and reloads itself when
anything changes.

## Testing

```console
$ nix build --file . tests.check -L      # 802 tests, ~110s, in a pure build sandbox
$ nix run --file . tests                 # all 961, if you have /dev/fuse
```

The mount tests need a real `/dev/fuse` and a namespace to mount in;
[test/README.md](test/README.md) has the `unshare` invocation and explains which
flag does what. Both halves run in CI.

The suite builds its own Postgres and PostgREST rather than talking to
`fswiki-dev`, which is somebody's working state.

`FSWIKI_COVERAGE=1` measures the child processes as well — the mount, the CLI
and the preview server each run in their own interpreter, and without it the
four largest modules in the project report zero however hard they are
exercised.

## Where the thinking is written down

The `docs/` directory is not API reference — it is the arguments, with the
measurements that settled them.

- [docs/rendering.md](docs/rendering.md) — why this cannot be a static site
  generator, why the render cache key is immutable, and how a link graph leaks
  if you are not careful.
- [docs/impersonation.md](docs/impersonation.md) — why acting as a *group* is
  not acting as a member of it, and why the refusal keys on function volatility
  rather than the HTTP verb.
- [docs/audit-trail.md](docs/audit-trail.md) — what the server knows by itself,
  and what is only ever a claim by software the user controls.
- [docs/change-notification.md](docs/change-notification.md) — a few bytes
  against six kilobytes, why the obvious token was wrong, and why a
  notification bridge must never carry content.
- [docs/search.md](docs/search.md) — why a text index and a row-level security
  policy want opposite things from the planner, and what that costs.
- [docs/attachments.md](docs/attachments.md) — why an attachment is a document
  row, where the size limit lives, and what a browser is told about a file
  somebody else uploaded.

## Status

Working and used, but early. Notably absent, and deliberately so: any identity
provider beyond "a JWT with a subject the database recognises", and attachments
in the mount — they are in the database and in the browser, but a binary file
that FUSE can carry is its own piece of work.

Requires PostgreSQL 16 or newer — `ltree` labels only accept hyphens and
non-ASCII from 16 on, and slugs depend on that.
