# Local dev stack

Real Postgres, real PostgREST, under process-compose. Nothing is mocked; the
client talks to the same thing production would.

    nix-build ./dev && ./result/bin/fswiki-dev

    ./result/bin/fswiki-dev reset     # wipe state and rebuild from scratch
    ./result/bin/fswiki-dev --tui=false   # headless, for scripts and CI

| | |
| --- | --- |
| PostgREST | `http://127.0.0.1:3000` |
| Postgres | `127.0.0.1:55432`, database `fswiki`, user `postgres`, trust auth |
| process-compose UI | `http://127.0.0.1:8080` |
| state | `$FSWIKI_ROOT/.dev` |

Override with `FSWIKI_HTTP_PORT`, `FSWIKI_PG_PORT`, `FSWIKI_PC_PORT`,
`FSWIKI_STATE`. Two checkouts can run side by side.

## Processes

    init  ->  postgres  ->  schema  ->  postgrest

`init` runs `initdb` and mints a JWT secret. `schema` creates the database and
loads `server/sql/*.sql`, then the test fixtures and `dev/seed.sql` — but only
when the database is absent, since the DDL is not idempotent. `reset` is how you
reload it.

## Tokens

    eval "$(fswiki-dev env)"
    curl -H "Authorization: Bearer $(fswiki-dev token bob)" \
      'http://127.0.0.1:3000/syncable_document?select=path&order=path'

The fixture users are `alice bob carol dave erin frank grace`. `fswiki-token`
signs `{role, iss, sub}` with the secret in `$FSWIKI_STATE/jwt-secret`, which is
exactly the shape `wiki.current_user_id()` resolves — no OIDC provider needed
until there is one.

The secret survives restarts and is regenerated only by `reset`.

## What the fixtures are for

`server/test/010_fixtures.sql` builds the ACL shapes the test suite asserts on,
so dev and test never drift. `dev/seed.sql` layers real markdown and a deeper
`public/guide/` subtree on top, and publishes second revisions so there is
history to look at.

The tree is deliberately different for every user, which is the fastest way to
tell whether a client is honouring the server or quietly caching someone else's
view:

| user | sees |
| --- | --- |
| `alice` | everything; superuser |
| `bob` | public + engineering; **`secret-plans` readable but not syncable** |
| `carol` | denied across engineering, except an explicit allow on `onboarding` |
| `dave` | owns `locked` and is denied everything on it, but keeps `grant` |
| `erin` | nothing at all — in no group, and the ACL is a closed world |
| `grace` | auditor; the `public` grant is no-propagate, so it stops one level down |

Two edge cases worth knowing about, both intentional:

- `root.public.unpublished` is a document with **no revisions**. `version`,
  `size` and `content` all come back null. A client that assumes every file has
  content will trip over it.
- `root.engineering.secret-plans` is in `current_document` but not in
  `syncable_document`. Any client that mirrors to disk must read the latter.

## Publishing from SQL

`dev/seed.sql` defines `pg_temp.publish()`, which closes the live interval and
opens the next. Two things it has to get right, and both bit during development:

- `now()` is the *transaction* timestamp and does not advance, so closing a
  revision opened in the same transaction yields an empty range — whose
  `lower()` is null, which the immutability trigger reads as a rewrite.
- the successor must open exactly where its predecessor closed, not at `now()`,
  or the two overlap and the exclusion constraint rejects it.

`wiki.push()` does the same dance; this is the smallest illustration of it.
