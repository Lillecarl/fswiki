"""Fixtures for the fswiki test suite.

The suite builds its own stack: a throwaway Postgres cluster, the schema, the
fixtures, and a PostgREST in front of them. It does not talk to `fswiki-dev`,
which is somebody's working state and would make the results depend on what
they had been doing.

Async tests run under the **anyio** pytest plugin (`pytest.mark.anyio`), not
pytest-asyncio: the client is written against anyio so that the same code runs
under trio inside the FUSE mount and under asyncio everywhere else, and the
plugin that can express that is the one to test it with. See
`anyio_backend` below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
ISSUER = "https://idp.test"

# Long enough for a mount's poll plus a shipper interval; short enough that a
# genuine failure does not look like a hang.
SETTLE = 15.0


# ---------------------------------------------------------------------------
# anyio
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def anyio_backend():
    """The backend every async test runs on unless it says otherwise.

    Session-scoped because async fixtures may be broader than a function, and
    the plugin requires this fixture to be at least as broad as they are.

    One module overrides it to run across both backends. Doing that everywhere
    would double a suite that spends its time waiting on a filesystem, to
    re-prove a property that belongs to one module's worth of code.
    """
    return "asyncio"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, *, timeout: float = SETTLE, interval: float = 0.1,
             what: str = "condition"):
    """Poll until `predicate` returns something truthy, then return it.

    Everything here is eventually-consistent by design — the mount polls, the
    audit shipper batches — so the choice is between sleeping for the worst
    case and waiting for the thing itself. Waiting is both faster and the only
    version that fails with a useful message.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} "
                         f"(last value: {last!r})")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_jwt(secret: str, subject: str, *, role: str = "fswiki_user") -> str:
    """An HS256 token PostgREST will accept.

    Twelve lines rather than a pyjwt dependency, and it keeps the whole token
    visible at the point where a test would want to argue about a claim.
    """
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "role": role, "iss": ISSUER, "sub": subject,
        "iat": now, "exp": now + 3600,
    }).encode())
    signing = f"{header}.{payload}".encode()
    sig = _b64(hmac.new(secret.encode(), signing, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------

@dataclass
class Stack:
    url: str
    pg_port: int
    datadir: Path
    secret: str
    procs: list = field(default_factory=list)

    # -- database ----------------------------------------------------------

    def psql(self, sql: str, *, db: str = "fswiki") -> str:
        """Run SQL as the owner and return the single-column result, trimmed.

        As the *owner*, so it bypasses RLS. That is deliberate: a test asserting
        what the server actually stored must not be filtered by the policy it is
        trying to check.

        **Trimmed**, which matters: psql's unaligned output drops the trailing
        newline, so this is the wrong way to fetch a document body. Use
        `content()` for anything whose exact bytes are the point.
        """
        out = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", str(self.pg_port), "-U", "postgres",
             "-d", db, "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise AssertionError(f"psql failed: {out.stderr.strip()}\n{sql}")
        return out.stdout.strip()

    def scalar(self, sql: str) -> str:
        return self.psql(sql)

    def count(self, sql: str) -> int:
        value = self.psql(sql)
        return int(value) if value else 0

    def exec(self, sql: str) -> None:
        self.psql(sql)

    # -- identity ----------------------------------------------------------

    def token(self, user: str, *, role: str = "fswiki_user") -> str:
        return mint_jwt(self.secret, user, role=role)

    def who(self, name: str) -> str:
        return self.scalar(f"select id from wiki.principal where name = '{name}'")

    def doc(self, path: str) -> str:
        found = self.scalar(f"select id from wiki.document where path = '{path}'")
        assert found, f"no document at {path}"
        return found

    def tip(self, path: str) -> int:
        return int(self.scalar(
            "select version from wiki.document_version v "
            "join wiki.document d on d.id = v.document_id "
            f"where d.path = '{path}' and upper_inf(v.valid)"))

    def content(self, path: str, *, user: str = "bob") -> str:
        """A document's published body, byte for byte, over HTTP.

        Not through psql: `psql -qAt` strips the trailing newline, and a test
        that rebuilds a file from a body missing its last byte measures its own
        truncation rather than the thing it meant to.
        """
        rows = http(f"{self.url}/current_document?select=content&path=eq.{path}",
                    token=self.token(user)).json
        assert rows, f"no published revision at {path}"
        return rows[0]["content"]

    def env(self, user: str = "bob", **extra) -> dict:
        """A child process's environment, pointed at this stack."""
        env = dict(os.environ)
        env["FSWIKI_URL"] = self.url
        env["FSWIKI_TOKEN"] = self.token(user)
        env.update(extra)
        return env


# Mounting FUSE needs a private mount namespace on some hosts, which means
# running the suite under `unshare --map-root-user` -- and Postgres refuses to
# run as root. The two requirements are not actually in conflict: map a subuid
# range as well, and the cluster can run as an ordinary uid inside a namespace
# whose root we are. See test/README.md for the invocation.
#
# None when we are already unprivileged, in which case nothing is dropped.
PG_UID = 1000 if os.geteuid() == 0 else None


def _as_postgres(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    if PG_UID is not None:
        kwargs.setdefault("user", PG_UID)
        kwargs.setdefault("group", PG_UID)
    return subprocess.run(cmd, **kwargs)


def _require(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        pytest.exit(f"not on PATH: {', '.join(missing)}. "
                    f"Run the suite through `nix run --file . tests`.", returncode=1)


@pytest.fixture(scope="session")
def stack():
    """A Postgres + PostgREST built from the repo, torn down afterwards.

    Session-scoped: initdb is seconds and everything after it is milliseconds,
    so one cluster per run is the right granularity. Per-test isolation comes
    from `clean` below, which empties the tables a test can dirty.
    """
    _require("initdb", "pg_ctl", "psql", "createdb", "postgrest")

    # Explicitly /tmp rather than TMPDIR: pytest points TMPDIR at a directory
    # only the current user may traverse, and the cluster may be running as a
    # different one.
    tmp = Path(tempfile.mkdtemp(prefix="fswiki-test-", dir="/tmp"))
    datadir = tmp / "pgdata"
    pg_port = free_port()
    http_port = free_port()
    secret = base64.b64encode(os.urandom(48)).decode()

    if PG_UID is not None:
        tmp.chmod(0o755)
        subprocess.run(["chown", "-R", f"{PG_UID}:{PG_UID}", str(tmp)], check=True)

    _as_postgres(["initdb", "-D", str(datadir), "-U", "postgres", "--auth=trust"],
                 check=True, capture_output=True)
    _as_postgres(
        ["pg_ctl", "-D", str(datadir), "-l", str(tmp / "postgres.log"),
         "-o", f"-h 127.0.0.1 -p {pg_port} -k '' -c log_min_messages=warning",
         "start", "-w"],
        check=True, capture_output=True)

    stack = Stack(url=f"http://127.0.0.1:{http_port}", pg_port=pg_port,
                  datadir=datadir, secret=secret)
    postgrest = None
    try:
        subprocess.run(["createdb", "-h", "127.0.0.1", "-p", str(pg_port),
                        "-U", "postgres", "fswiki"], check=True, capture_output=True)

        for sql in schema_files():
            _load(pg_port, sql)
        # The same fixtures the SQL suite asserts against, so a number measured
        # in one suite means the same thing in the other.
        _load(pg_port, ROOT / "server" / "test" / "010_fixtures.sql")
        _load(pg_port, ROOT / "dev" / "seed.sql")

        postgrest = _start_postgrest(stack, http_port, tmp / "postgrest.log")
        stack.procs.append(postgrest)
        yield stack
    finally:
        if postgrest is not None:
            postgrest.terminate()
            try:
                postgrest.wait(timeout=5)
            except subprocess.TimeoutExpired:
                postgrest.kill()
        _as_postgres(["pg_ctl", "-D", str(datadir), "stop", "-m", "immediate"],
                     capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _start_postgrest(stack: Stack, port: int, log_path: Path) -> subprocess.Popen:
    """One PostgREST against `stack`'s cluster, ready to serve queries.

    Factored out because a test that wants to know what a client does when the
    server goes away needs a PostgREST it is allowed to kill, and killing the
    session's own would take every later test with it.
    """
    env = dict(os.environ)
    env.update(
        PGRST_DB_URI=f"postgres://fswiki_authenticator@127.0.0.1:{stack.pg_port}/fswiki",
        PGRST_DB_SCHEMAS="wiki",
        PGRST_DB_ANON_ROLE="fswiki_anon",
        PGRST_JWT_SECRET=stack.secret,
        PGRST_SERVER_HOST="127.0.0.1",
        PGRST_SERVER_PORT=str(port),
        PGRST_DB_POOL="4",
        # The only door into impersonation; without it half the suite is
        # testing a feature that is not switched on.
        PGRST_DB_PRE_REQUEST="wiki.pre_request",
    )
    log = open(log_path, "a")
    proc = subprocess.Popen(["postgrest"], env=env, stdout=log, stderr=log)

    def up():
        if proc.poll() is not None:
            raise AssertionError("postgrest exited: " + log_path.read_text())
        # A real query as a real user, not just "the socket answers". PostgREST
        # accepts connections while its schema cache is still loading and
        # returns 503 to everything, which arrives at the client as an ordinary
        # failed request several tests later.
        try:
            return http(f"http://127.0.0.1:{port}/syncable_document?select=id&limit=1",
                        token=stack.token("bob"), timeout=2).code == 200
        except urllib.error.URLError:
            # Not listening yet. Only a refused connection is "not yet"; a
            # server that answers badly is a failure and must not be waited out.
            return False

    wait_for(up, timeout=30, what=f"postgrest on {port} to serve queries")
    return proc


@dataclass
class Spare:
    """A PostgREST of one test's own, which it may stop and start again."""
    url: str
    log: Path
    _stack: Stack
    _port: int
    _proc: subprocess.Popen | None

    def stop(self) -> None:
        """Take the server away, the way a laptop lid does."""
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        # Not just "the process is gone": until the socket is actually closed a
        # client can still connect and hang, and a test that raced that would
        # be flaky in the least reproducible way there is.
        wait_for(lambda: not answers(self.url + "/"),
                 what="the port to stop answering")

    def start(self) -> None:
        if self._proc is None:
            self._proc = _start_postgrest(self._stack, self._port, self.log)


@pytest.fixture
def db_outage(stack):
    """Stop the *cluster*, and always bring it back before the next test.

    A layer below `spare`. There the socket is gone and every request is a
    connection refused; here PostgREST is still listening and still answering —
    with 503s and a schema cache it cannot load. Clients meet those two through
    completely different code paths, and one of them is easy to forget.

    The session's own cluster, because a second one would mean a second initdb
    and a second schema load for one module's worth of tests. The contract is
    the `finally`: whatever the test does, the stack is serving queries again
    before the fixture returns.
    """
    stopped = False

    def stop():
        nonlocal stopped
        _as_postgres(["pg_ctl", "-D", str(stack.datadir), "stop", "-m", "fast"],
                     check=True, capture_output=True)
        stopped = True

    def start():
        nonlocal stopped
        if not stopped:
            return
        _as_postgres(
            ["pg_ctl", "-D", str(stack.datadir),
             "-l", str(stack.datadir.parent / "postgres.log"),
             "-o", f"-h 127.0.0.1 -p {stack.pg_port} -k '' "
                   f"-c log_min_messages=warning", "start", "-w"],
            check=True, capture_output=True)
        stopped = False
        # PostgREST backs off between reconnection attempts, and waiting that
        # out would put tens of seconds into the suite for nothing. SIGUSR1 is
        # its "reload the schema cache" signal, which reconnects first.
        for proc in stack.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGUSR1)
        wait_for(lambda: _serving(stack), timeout=60,
                 what="postgrest to recover from the outage")

    outage = SimpleNamespace(stop=stop, start=start)
    try:
        yield outage
    finally:
        start()


def _serving(stack: Stack) -> bool:
    try:
        return http(stack.url + "/syncable_document?select=id&limit=1",
                    token=stack.token("bob"), timeout=2).code == 200
    except urllib.error.URLError:
        return False


@pytest.fixture
def spare(stack, tmp_path):
    """A second PostgREST on the same database, for tests that kill one."""
    port = free_port()
    log = tmp_path / "spare-postgrest.log"
    s = Spare(url=f"http://127.0.0.1:{port}", log=log, _stack=stack,
              _port=port, _proc=_start_postgrest(stack, port, log))
    try:
        yield s
    finally:
        s.stop()


def _load(port: int, path: Path, *, db: str = "fswiki") -> None:
    out = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-d", db,
         "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"loading {path.name} failed:\n{out.stderr}")


@pytest.fixture
def clean(stack):
    """Empty everything a test can dirty, before it runs.

    Before rather than after, so a failed test leaves its wreckage to be looked
    at. Published revisions are deliberately not reset: rolling the tree back
    would mean rebuilding the database per test, and tests that publish are
    written to use paths of their own instead.

    Impersonation *grants* are not cleared either, and for a sharper reason: a
    grant is configuration, not state a test dirties. A mount started with
    `--as` outlives the test that started it and keeps polling, so revoking
    behind its back would make some unrelated later test fail with a 403 from a
    filesystem it never asked about.
    """
    stack.exec("""
        delete from wiki.draft;
        delete from wiki.access_event;
        delete from wiki.impersonation_event;
    """)
    return stack


# ---------------------------------------------------------------------------
# The client library
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(stack):
    """A `Client` as bob, closed afterwards.

    Async, and therefore only usable from a test marked `anyio` — which is the
    point of using the anyio plugin rather than driving `asyncio.run` by hand
    in each test.
    """
    from fswiki_core.client import Client

    made = []

    async def make(user: str = "bob", **kwargs):
        c = Client(stack.url, stack.token(user), **kwargs)
        made.append(c)
        return c

    try:
        yield make
    finally:
        for c in made:
            await c.aclose()


# ---------------------------------------------------------------------------
# Child processes: the CLI, the mount, the preview server
# ---------------------------------------------------------------------------

@dataclass
class Run:
    code: int
    out: str

    def __contains__(self, needle: str) -> bool:
        return needle in self.out

    def __repr__(self) -> str:  # what pytest prints on a failed assert
        return f"<exit {self.code}>\n{self.out}"


@pytest.fixture
def cli(stack):
    """Run the real `fswiki` binary. Returns exit code and combined output.

    A subprocess rather than calling `main()` in-process, because argument
    parsing, exit codes and stderr are part of what a CLI is, and importing
    around them tests something else.
    """
    _require("fswiki")

    def run(*args: str, user: str = "bob", **env_extra) -> Run:
        proc = subprocess.run(
            ["fswiki", "--no-colour", *args],
            capture_output=True, text=True, env=stack.env(user, **env_extra))
        return Run(proc.returncode, proc.stdout + proc.stderr)

    return run


@dataclass
class Mount:
    path: Path
    proc: subprocess.Popen
    log: Path

    def __truediv__(self, rel: str) -> Path:
        return self.path / rel

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text()

    def write(self, rel: str, text: str) -> None:
        (self.path / rel).write_text(text)

    def stop(self) -> None:
        """Unmount and wait for the process, for tests about what survives it.

        Idempotent, so the factory's own teardown can run over a mount a test
        has already stopped.
        """
        subprocess.run(["fusermount3", "-u", str(self.path)], capture_output=True)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="session")
def mount_factory(stack, tmp_path_factory):
    """Start a mount, wait for it, and always unmount.

    Session-scoped so that mounts can be shared; each one is torn down when the
    session ends. A leaked mount is worse than a leaked process — it leaves a
    directory that hangs every `ls` — so the unmount runs even if the process
    is already dead.
    """
    _require("fswiki-mount", "fusermount3")
    if not os.path.exists("/dev/fuse"):
        # A build sandbox has null, zero, random, tty and little else in /dev,
        # and no amount of unsharing conjures a device node that is not there.
        # Skipping says that; the alternative is every mount test failing with
        # whatever libfuse says about an open() it could not make.
        pytest.skip("no /dev/fuse; mount tests need one")
    live: list[Mount] = []

    def start(*flags: str, user: str = "bob", poll: float = 0.25,
              ttl: float = 0.2) -> Mount:
        # A short kernel TTL, which is a test-harness concern rather than a
        # sensible default. `--ttl` is how long the kernel is told it may cache
        # lookups and attributes without asking us, so at the shipped 5 seconds
        # every "has the mount noticed yet?" costs up to five seconds of stale
        # cache -- and the merge tests, which are nothing but that question,
        # took two thirds of the suite's wall clock waiting for it. Serving a
        # getattr from the tree we already hold is free; only `poll` decides how
        # often the server is asked anything -- and a poll is a few bytes, so
        # four a second costs less than one manifest fetch would.
        base = tmp_path_factory.mktemp("mnt")
        log = base.parent / f"{base.name}.log"
        handle = open(log, "w")
        proc = subprocess.Popen(
            ["fswiki-mount", str(base), "--poll", str(poll), "--ttl", str(ttl),
             *flags],
            stdout=handle, stderr=handle, env=stack.env(user))

        def mounted():
            if proc.poll() is not None:
                raise AssertionError(f"mount exited:\n{log.read_text()}")
            return os.path.ismount(base)

        wait_for(mounted, timeout=30, what=f"{base} to be mounted")
        m = Mount(base, proc, log)
        live.append(m)
        return m

    try:
        yield start
    finally:
        for m in live:
            m.stop()


@pytest.fixture(scope="session")
def mount(mount_factory):
    """The ordinary mount, as bob. Shared by every test that needs a tree.

    Mounting costs a manifest fetch and a FUSE handshake, and nothing a test
    does to it cannot be undone by clearing drafts — so one mount for the
    session, and `clean` for isolation.
    """
    return mount_factory()


@pytest.fixture
def preview(stack, mount):
    """A `fswiki preview` on its own port, stopped afterwards."""
    _require("fswiki")
    port = free_port()
    log = Path(tempfile.mkstemp(prefix="preview-", suffix=".log")[1])
    handle = open(log, "w")
    proc = subprocess.Popen(
        ["fswiki", "preview", "--port", str(port)],
        stdout=handle, stderr=handle, env=stack.env("bob"))
    base = f"http://127.0.0.1:{port}"

    def up():
        if proc.poll() is not None:
            raise AssertionError(f"preview exited:\n{log.read_text()}")
        return answers(base + "/")

    try:
        wait_for(up, timeout=30, what="preview to answer")
        yield base
    finally:
        # SIGINT, not SIGTERM: it is how the server says to stop it ("ctrl-c to
        # stop"), so it is the path worth exercising -- and Python turns it
        # into a KeyboardInterrupt that unwinds, where the default SIGTERM
        # handler kills the process outright and runs no atexit hooks. Under
        # FSWIKI_COVERAGE that difference is the whole of preview.py's
        # measurement, which is written by one of those hooks.
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Raw HTTP
# ---------------------------------------------------------------------------

@dataclass
class Response:
    code: int
    body: str
    headers: dict

    @property
    def json(self):
        return json.loads(self.body)

    @property
    def error(self) -> dict:
        """PostgREST's error object, or {} — so a test can name the SQLSTATE."""
        try:
            value = self.json
        except ValueError:
            return {}
        return value if isinstance(value, dict) and "code" in value else {}

    def __contains__(self, needle: str) -> bool:
        return needle in self.body


def schema_files() -> list[Path]:
    """Every schema file, in load order, across the three tracks.

    tables/ is state and loads once; runtime/ is dropped and replayed on every
    deploy; seed/ follows runtime because the root document's path is computed
    by a trigger. A fresh database wants all three, in that order. See
    server/fswiki_server/migrate.py, which is what does this in production.
    """
    root = ROOT / "server" / "schema"
    return [f for track in ("tables", "runtime", "seed")
            for f in sorted((root / track).glob("*.sql"))]


def http(url: str, *, method: str = "GET", token: str | None = None,
         body=None, headers: dict | None = None, timeout: float = 10.0) -> Response:
    """One request, with the status kept rather than raised.

    urllib turns a 4xx into an exception, which is the wrong shape here: a
    refusal is usually the thing under test, and its body carries the reason.
    """
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read().decode(), dict(r.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read().decode(), dict(exc.headers))


def answers(url: str) -> bool:
    """True once something is listening at `url` and answering 200.

    Separate from `http` on purpose: a connection refused is not a response and
    must not be dressed up as one, or a test asserting on a status code would
    quietly be asserting about a server that never started. This is the one
    place that wants "not yet" instead of an exception — waiting for a child
    process to come up.
    """
    try:
        return http(url).code == 200
    except urllib.error.URLError:
        return False


@pytest.fixture
def rest(stack):
    """PostgREST, as a named user, without a client library in the way.

    `user=None` sends no Authorization header at all, which is the only way to
    exercise what PostgREST does with an unauthenticated request: it switches
    to db-anon-role and fswiki_anon's grants decide the rest. Dropping to that
    role in psql tests the grants but not PostgREST's half of the arrangement,
    and the two have to agree.
    """
    def call(path: str, *, user: str | None = "bob", method: str = "GET",
             body=None, headers: dict | None = None) -> Response:
        return http(stack.url + path, method=method,
                    token=stack.token(user) if user else None,
                    body=body, headers=headers)
    return call
