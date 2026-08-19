"""The wiki, for people who are not running a client.

A browser talks to this; the CLI and the FUSE mount talk to PostgREST
directly. It is not a proxy in front of them, and it holds no identity of its
own: a visitor's token is passed through, and Postgres decides what comes back.

**Identity arrives through PostgREST, and only through PostgREST.** Every
policy in 050_rls.sql keys off wiki.current_user_id(), which reads the
`request.jwt.claims` GUC, and that GUC is set by PostgREST after it has
verified the token's signature. A connection this process opens to Postgres
itself has no such GUC, so RLS sees an anonymous caller -- or, if the
connection were a privileged role, no RLS at all. Either way the answer would
be wrong, and one of those two ways is wrong in the direction that publishes
the wiki.

So the rule for talking to Postgres directly from here: it may only be for
things that do not depend on who is asking. Migrations at startup. A render
cache keyed on (document_id, version, renderer), which is identical for every
reader by construction. A health check. Anything whose answer varies by
visitor goes over PostgREST with that visitor's token, every time.

psycopg is the driver, and the same package covers both shapes: a synchronous
Connection for the startup phases, which are sequential and have no event loop
to block, and AsyncConnection for anything that ever runs inside a request.
asyncpg would be a second driver for one job, and asyncio-only besides, where
the rest of this project is written against anyio.
"""

__all__ = ["config", "migrate", "postgrest"]
