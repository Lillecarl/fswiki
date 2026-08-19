"""What the server needs to know, and where it comes from.

Environment first, because that is how it will be deployed, with the command
line able to override any of it for the times you are holding it by hand.

One thing is deliberately absent: a way to point at a Postgres to *start*.
Postgres is infrastructure you have; PostgREST is an implementation detail of
this program, and the difference is why one arrives as a URL and the other as
a binary to run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# A 64-bit constant, held for the whole of the migration phase. Two servers
# starting at once -- a restart overlapping its predecessor, a rolling deploy,
# or someone running it twice -- would otherwise race to load the same schema.
# Written as a literal rather than derived from a string, so it cannot drift
# between versions and quietly stop excluding anybody.
MIGRATION_LOCK = 0x6673776_96B690001

# How a PostgREST is told its schema cache is stale.
#
# PostgREST builds the cache when it connects and does not notice DDL, so the
# runtime rebuild has to say so. A signal only reaches a child; a notification
# reaches every instance connected to the database, whoever started it -- a
# rolling deploy's other half, a `fswiki-dev` someone left running, or a person
# applying the schema by hand from psql.
#
# Both ends are named here because they have to be the same two strings in two
# places, and a channel that matches at only one end is a silence rather than
# an error. These are PostgREST's own defaults; setting them explicitly is what
# stops a default moving under us.
PGRST_CHANNEL = "pgrst"
PGRST_RELOAD_SCHEMA = "reload schema"


class ConfigError(Exception):
    """Something required is missing, said in a way that names the fix."""


def _schema_dir(explicit: str | None) -> Path:
    """Where the .sql files are.

    Three answers in order of confidence: told, installed beside the package,
    or found by walking up from the working directory the way the test runner
    does. The last is for a checkout; the Nix wrapper supplies the first.
    """
    if explicit:
        return Path(explicit)
    installed = Path(__file__).resolve().parent / "schema"
    if installed.is_dir():
        return installed
    for base in (Path.cwd(), *Path.cwd().parents):
        candidate = base / "server" / "schema"
        if candidate.is_dir():
            return candidate
    raise ConfigError(
        "cannot find the schema: set FSWIKI_SCHEMA_DIR, or run from a checkout")


@dataclass(frozen=True)
class Config:
    database_url: str
    schema_dir: Path

    # This server.
    host: str = "127.0.0.1"
    port: int = 8080

    # PostgREST, which this process starts and supervises. Bound separately
    # and reachable in its own right: the CLI and the mount talk to it, not
    # through here.
    postgrest_bin: str = "postgrest"
    postgrest_host: str = "127.0.0.1"
    postgrest_port: int = 3000
    postgrest_jwt_secret: str | None = None
    # PostgREST connects as fswiki_authenticator, which can do nothing itself
    # and holds the two nologin roles it switches between. The migration URL
    # above needs DDL rights and this one must not have them, so they are two
    # settings. Falling back is a convenience for a checkout, not a default to
    # deploy: a PostgREST connected as the owner of the tables would bypass
    # every policy in 050_rls.sql, because owners are not subject to RLS.
    postgrest_database_url: str | None = None

    @property
    def postgrest_url(self) -> str:
        return f"http://{self.postgrest_host}:{self.postgrest_port}"

    @property
    def postgrest_db_uri(self) -> str:
        return self.postgrest_database_url or self.database_url

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = os.environ if env is None else env
        url = e.get("FSWIKI_DATABASE_URL")
        if not url:
            raise ConfigError(
                "FSWIKI_DATABASE_URL is required, e.g. "
                "postgres://user@host:5432/fswiki")
        return cls(
            database_url=url,
            schema_dir=_schema_dir(e.get("FSWIKI_SCHEMA_DIR")),
            host=e.get("FSWIKI_HOST", cls.host),
            port=int(e.get("FSWIKI_PORT", cls.port)),
            postgrest_bin=e.get("FSWIKI_POSTGREST_BIN", cls.postgrest_bin),
            postgrest_host=e.get("FSWIKI_POSTGREST_HOST", cls.postgrest_host),
            postgrest_port=int(e.get("FSWIKI_POSTGREST_PORT", cls.postgrest_port)),
            postgrest_jwt_secret=e.get("FSWIKI_JWT_SECRET"),
            postgrest_database_url=e.get("FSWIKI_POSTGREST_DATABASE_URL"),
        )
