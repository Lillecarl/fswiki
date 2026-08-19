"""PostgREST, as a child process.

The second startup phase. It is a child rather than a sibling under a process
supervisor for one concrete reason: **the schema cache**. PostgREST builds it
when it connects and does not notice DDL afterwards, so the process that
changes the schema has to be able to tell it, and being its parent makes that
a signal.

A signal is not the whole answer, though, and it is worth being clear about
which half it is. It reaches the instance we own and nothing else. Every other
PostgREST on the same database -- the other half of a rolling deploy, a
`fswiki-dev` someone left running -- hears about a rebuild over the `pgrst`
notification channel instead, which `migrate()` writes to inside the migration
transaction. See config.PGRST_CHANNEL.

It is not proxied. The CLI and the FUSE mount talk to PostgREST directly, so
it binds an address of its own and this process only owns its lifetime.

Nothing here decides anything about permissions. The environment below names
the anonymous role and the pre-request hook; which rows either of them can see
is 050_rls.sql's business and is not repeated in this file.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

from .config import PGRST_CHANNEL, Config

log = logging.getLogger(__name__)


class PostgrestError(Exception):
    """PostgREST would not start, or stopped when it should not have."""


def environment(config: Config) -> dict[str, str]:
    """The whole of PostgREST's configuration, as environment.

    Separate from start() so a test can assert on it without running anything,
    and so the two settings that are security rather than plumbing --
    db-anon-role and db-pre-request -- are somewhere they can be pointed at.
    """
    env = dict(os.environ)
    env.update({
        "PGRST_DB_URI": config.postgrest_db_uri,
        "PGRST_DB_SCHEMAS": "wiki",
        # Unauthenticated requests arrive as this role, which holds exactly the
        # read path and nothing else. See the anonymous grants in
        # 060_roles.sql and the allow-list in server/test/070_public_test.sql.
        "PGRST_DB_ANON_ROLE": "fswiki_anon",
        "PGRST_SERVER_HOST": config.postgrest_host,
        "PGRST_SERVER_PORT": str(config.postgrest_port),
        # Runs inside every request's transaction, before anything else, and is
        # the only door into impersonation. See 100_impersonation.sql.
        "PGRST_DB_PRE_REQUEST": "wiki.pre_request",
        "PGRST_OPENAPI_MODE": "follow-privileges",
        # How an instance hears that the schema moved. Both are PostgREST's
        # own defaults; they are set anyway because the notification at the
        # end of migrate() is the only thing that reaches an instance we did
        # not start, and a default that moves would break it in silence.
        "PGRST_DB_CHANNEL": PGRST_CHANNEL,
        "PGRST_DB_CHANNEL_ENABLED": "true",
    })
    if config.postgrest_jwt_secret:
        env["PGRST_JWT_SECRET"] = config.postgrest_jwt_secret
    return env


def answers(url: str, timeout: float = 1.0) -> bool:
    """True once something is listening and answering.

    A refused connection is not a response and must not be dressed up as one:
    a caller waiting for readiness needs "not yet" and a caller checking health
    needs "no", and conflating them turns a server that never started into a
    server that returned an error.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as exc:
        # A 4xx still means PostgREST is up and talking; readiness is about the
        # process, not about whether the root path happens to be permitted.
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


class Postgrest:
    """One PostgREST process, owned for as long as this object is."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return self._config.postgrest_url

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, *, timeout: float = 30.0) -> None:
        """Start it, and do not return until it is answering.

        Returning early would hand the serve phase a socket that is not
        listening yet, and the first visitor would get a connection refused
        that looks like a bug in this program rather than a race in its
        startup.
        """
        if self.running:
            raise PostgrestError("already started")
        log.info("starting postgrest on %s", self.url)
        try:
            self._proc = subprocess.Popen(
                [self._config.postgrest_bin], env=environment(self._config))
        except OSError as exc:
            # The commonest deployment mistake, and a bare FileNotFoundError
            # from Popen names the binary without saying what wanted it.
            raise PostgrestError(
                f"cannot run {self._config.postgrest_bin!r}: {exc}. "
                f"Set FSWIKI_POSTGREST_BIN, or put postgrest on PATH") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise PostgrestError(
                    f"postgrest exited with {self._proc.returncode} before it "
                    f"answered; check the connection string and fswiki_authenticator")
            if answers(self.url):
                log.info("postgrest is answering")
                return
            time.sleep(0.05)
        self.stop()
        raise PostgrestError(f"postgrest did not answer within {timeout}s")

    def reload(self) -> None:
        """Tell it the schema changed.

        PostgREST builds its schema cache on connect and does not notice DDL,
        so anything that runs the schema files again -- which is what the
        runtime half of a two-track migration does on every deploy -- has to
        follow them with this. Being its parent is what makes that a method
        call rather than a NOTIFY on a channel configured at both ends.

        Startup does not need it. The phases run migrate, then start, then
        serve, so this process's PostgREST connects after the schema has
        stopped moving and builds a cache of the new shape. This is for a
        rebuild that happens while we are already serving, and it is the
        cheaper of the two paths: synchronous, and not dependent on the
        notification channel being enabled at either end.

        **SIGUSR1, not SIGUSR2.** PostgREST reloads its schema cache on the
        first and its configuration on the second, which is the opposite way
        round to the guess; sending the wrong one logs "Config reloaded" and
        leaves every function added since startup answering 404. Measured, in
        test_it_notices_a_function_added_after_it_connected.
        """
        if not self.running:
            raise PostgrestError("not running")
        assert self._proc is not None
        log.info("reloading the postgrest schema cache")
        self._proc.send_signal(signal.SIGUSR1)

    def stop(self, *, timeout: float = 10.0) -> None:
        """Ask it to stop, then insist. Safe to call when it is not running."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("postgrest ignored SIGTERM; killing it")
                self._proc.kill()
                self._proc.wait(timeout=timeout)
        self._proc = None

    def __enter__(self) -> "Postgrest":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
