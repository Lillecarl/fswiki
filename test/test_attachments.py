"""Attachments: bytes with a path, and everything that follows from that.

The database half is proved in `server/test/110_attachment_test.sql`, over the
cross product of every fixture user. What is left for here is the part the SQL
suite cannot see from where it stands: what a browser is told about a file it
is being handed.

That is the whole of the security surface above the ACL. An attachment is
bytes one person uploaded, served from the same origin as everybody else's
pages, so the headers are not decoration — they are the difference between a
diagram and stored cross-site scripting. `attachment_headers` is pure, so it
is tested first and exhaustively; the rest goes through the real application.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from conftest import ROOT
from fswiki_core import naming
from fswiki_core.pages import (ATTACHMENT_CSP, HTML, INLINE, TEXT,
                               attachment_headers)
from fswiki_server.app import Application
from fswiki_server.config import Config

pytestmark = pytest.mark.anyio

# The smallest real PNG there is: 1x1, transparent.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100fdff03fa0000000049454e44ae426082")

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def headers(kind):
    return dict(attachment_headers(kind))


# ---------------------------------------------------------------------------
# What a browser is told
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [HTML, TEXT])
def test_a_page_gets_no_extra_headers(kind):
    """The site-wide set is right for a page. Adding a disposition to one
    would make every wiki page a download."""
    assert attachment_headers(kind) == []


@pytest.mark.parametrize("kind", sorted(INLINE))
def test_an_image_may_render_in_the_page(kind):
    assert headers(kind)["content-disposition"] == "inline"


@pytest.mark.parametrize("kind", [
    "image/svg+xml", "application/pdf", "text/html", "application/zip",
    "application/octet-stream", "application/xhtml+xml", "text/xml",
])
def test_everything_else_downloads_rather_than_renders(kind):
    """The rule that makes an SVG safe without banning it.

    `<img>` ignores `Content-Disposition`, so a diagram still draws in a page.
    A direct visit downloads the file instead of opening a document — and a
    document is the only place an SVG's `<script>` could ever run.
    """
    assert headers(kind)["content-disposition"] == "attachment"


@pytest.mark.parametrize("kind", sorted(INLINE) + ["image/svg+xml", "text/html"])
def test_every_attachment_carries_its_own_policy(kind):
    """Belt to the disposition's braces. `sandbox` with no `allow-scripts`
    stops a file opened as a document from running anything, before
    `Content-Disposition` gets a chance to."""
    assert headers(kind)["content-security-policy"] == ATTACHMENT_CSP
    assert "sandbox" in ATTACHMENT_CSP
    assert "allow-scripts" not in ATTACHMENT_CSP


def test_the_policy_allows_nothing_by_default():
    assert ATTACHMENT_CSP.startswith("default-src 'none'")


def test_no_filename_is_offered():
    """The file is served at its own path, so the last segment of the URL is
    already its name. A second copy here could disagree with the first."""
    assert "filename" not in headers("image/png")["content-disposition"]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_a_url_may_carry_the_extension():
    assert naming.from_display("public/logo.png") == "root.public.logo"


def test_and_may_leave_it_off():
    assert naming.from_display("public/logo") == "root.public.logo"


def test_a_binary_extension_now_means_something_in_the_mount():
    """The two extension maps merged when a file became a revision.

    While the mount could not carry bytes, `logo.png` written into a directory
    had to stay scratch -- otherwise it became a document claiming to be an
    image with text inside it. Writing one is as meaningful as writing a page
    now, so the split had nothing left to protect.
    """
    assert naming.parse_filename("logo.png") == ("logo", "image/png")
    assert naming.parse_filename("notes.md") == ("notes", "text/markdown")


def test_an_extension_the_wiki_does_not_serve_is_still_refused():
    with pytest.raises(ValueError):
        naming.from_display("public/report.tar.gz")


def test_the_filename_carries_the_type():
    assert naming.filename("logo", "image/png", False) == "logo.png"
    assert naming.filename("plan", "application/pdf", False) == "plan.pdf"


def test_an_unknown_type_gets_no_invented_extension():
    """The header says what it is either way. A guessed `.bin` would be a lie
    in the name a person saves."""
    assert naming.filename("thing", "application/x-nonsense", False) == "thing"


def test_which_bodies_are_bytes():
    """The one question that decides which column a revision fills, whether a
    merge is possible, and what the mount hands the kernel."""
    assert naming.is_binary_type("image/png")
    assert naming.is_binary_type("image/svg+xml")
    assert naming.is_binary_type("application/pdf")
    assert not naming.is_binary_type("text/markdown")
    assert not naming.is_binary_type("application/json")
    assert not naming.is_binary_type(None)


def test_an_unlisted_type_is_treated_as_bytes():
    """The safe direction: text treated as bytes round-trips exactly, and bytes
    treated as text do not survive the trip at all."""
    assert naming.is_binary_type("application/x-never-heard-of-it")


# ---------------------------------------------------------------------------
# Through the whole stack
# ---------------------------------------------------------------------------

@pytest.fixture
def config(stack):
    parsed = urllib.parse.urlsplit(stack.url)
    return Config(
        database_url=f"postgres://postgres@127.0.0.1:{stack.pg_port}/fswiki",
        schema_dir=ROOT / "server" / "schema",
        postgrest_host=parsed.hostname,
        postgrest_port=parsed.port,
    )


@pytest.fixture
async def browser(config):
    app = Application(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://wiki.test") as c:
        yield c
    await app.aclose()


@pytest.fixture
async def alice(stack):
    from fswiki_core.client import Client
    c = Client(stack.url, stack.token("alice"), tree="read")
    yield c
    await c.aclose()


@pytest.fixture
async def logo(alice):
    """A PNG in the public tree, retired afterwards."""
    await alice.attach("root.public.logo", "image/png", PNG)
    yield "public/logo"
    await alice.detach("root.public.logo")


def token(stack, who):
    return {"Authorization": f"Bearer {stack.token(who)}"}


async def test_the_bytes_come_back_exactly(browser, stack, logo):
    r = await browser.get(f"/{logo}.png", headers=token(stack, "alice"))
    assert r.status_code == 200
    assert r.content == PNG


async def test_the_hex_round_trip_does_not_corrupt_anything(alice):
    """PostgREST renders bytea the way Postgres does, so the client decodes it.
    Every byte value, to say the encoding is not lossy somewhere in the middle.
    """
    every = bytes(range(256))
    await alice.attach("root.public.everybyte", "application/octet-stream", every)
    try:
        row = await alice.document("root.public.everybyte")
        assert row["content_bytes"] == every
        assert row["size"] == 256
        assert row["is_binary"] is True
        assert row["content"] is None
        assert await alice.content(row["id"]) == every
    finally:
        await alice.detach("root.public.everybyte")


async def test_it_is_served_without_the_extension_too(browser, stack, logo):
    r = await browser.get(f"/{logo}", headers=token(stack, "alice"))
    assert r.status_code == 200
    assert r.content == PNG


async def test_the_type_and_disposition_are_right(browser, stack, logo):
    r = await browser.get(f"/{logo}.png", headers=token(stack, "alice"))
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == "inline"


async def test_the_response_carries_two_policies(browser, stack, logo):
    """Two `content-security-policy` headers, not one replacing the other. A
    browser enforces every policy it is given, so the stricter one wins and
    neither has to know about the other."""
    r = await browser.get(f"/{logo}.png", headers=token(stack, "alice"))
    policies = r.headers.get_list("content-security-policy")
    assert len(policies) == 2
    assert ATTACHMENT_CSP in policies


async def test_a_page_may_show_it(browser, stack, logo):
    """The site-wide CSP has to allow an image from this origin, or an
    attachment is invisible in the page that references it."""
    r = await browser.get("/", headers=token(stack, "alice"))
    policy = r.headers.get_list("content-security-policy")[0]
    assert "img-src 'self'" in policy


async def test_an_svg_is_served_as_an_svg_but_downloads(browser, stack, alice):
    """Both halves of the rule. The type is right, so `<img>` draws it; the
    disposition is `attachment`, so a direct visit never opens it as a
    document -- which is the only place its `<script>` could run."""
    await alice.attach("root.public.diagram", "image/svg+xml", SVG)
    try:
        r = await browser.get("/public/diagram.svg", headers=token(stack, "alice"))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/svg+xml"
        assert r.headers["content-disposition"] == "attachment"
        assert "sandbox" in " ".join(r.headers.get_list("content-security-policy"))
    finally:
        await alice.detach("root.public.diagram")


async def test_nothing_is_cached(browser, stack, logo):
    """Same reason as a page: a 304 is a read the trail never saw. An image is
    a subresource, but it is one whose ACL can change under it."""
    r = await browser.get(f"/{logo}.png", headers=token(stack, "alice"))
    assert r.headers["cache-control"] == "no-store"


async def test_head_carries_the_length_but_no_body(browser, stack, logo):
    r = await browser.head(f"/{logo}.png", headers=token(stack, "alice"))
    assert r.headers["content-length"] == str(len(PNG))
    assert r.content == b""


# --- and the ACL still decides ---------------------------------------------

async def test_a_file_you_may_not_read_is_a_file_that_is_not_there(browser, stack):
    """`root.locked` is alice's alone.

    The assertion is the same address before and after the file exists, rather
    than two different addresses: a 404 names the path you asked for, so two
    paths differ for a reason that is not a disclosure. What must not differ is
    what bob sees when a file he may not read appears -- uploading one must be
    invisible to him, byte for byte.
    """
    from fswiki_core.client import Client
    owner = Client(stack.url, stack.token("alice"), tree="read")
    route = "/locked/secret-chart.png"
    try:
        before = await browser.get(route, headers=token(stack, "bob"))
        await owner.attach("root.locked.secret-chart", "image/png", PNG)
        after = await browser.get(route, headers=token(stack, "bob"))
        mine = await browser.get(route, headers=token(stack, "alice"))

        assert mine.status_code == 200 and mine.content == PNG
        assert after.status_code == before.status_code == 404
        assert after.content == before.content
    finally:
        await owner.detach("root.locked.secret-chart")
        await owner.aclose()


async def test_an_anonymous_visitor_gets_nothing_they_were_not_granted(browser,
                                                                       logo):
    r = await browser.get(f"/{logo}.png")
    assert r.status_code == 404


# --- the limit --------------------------------------------------------------

async def test_the_limit_is_the_wikis_and_the_client_asks_for_it(alice):
    assert await alice.max_attachment_bytes() == 10485760


async def test_a_file_over_the_limit_is_refused_by_the_database(alice, stack):
    """The client is told the number, but the refusal is the database's. A cap
    the CLI enforces is a cap psql does not."""
    from fswiki_core.client import PostgrestError
    stack.exec("update wiki.setting set value = '32' "
               "where key = 'max_attachment_bytes'")
    try:
        with pytest.raises(PostgrestError):
            await alice.attach("root.public.toobig", "image/png", b"x" * 33)
        assert await alice.document("root.public.toobig") is None
    finally:
        stack.exec("update wiki.setting set value = '10485760' "
                   "where key = 'max_attachment_bytes'")


async def test_the_refusal_names_the_number(alice, stack):
    """A refusal a person cannot act on is a bug."""
    from fswiki_core.client import PostgrestError
    stack.exec("update wiki.setting set value = '32' "
               "where key = 'max_attachment_bytes'")
    try:
        with pytest.raises(PostgrestError) as caught:
            await alice.attach("root.public.toobig", "image/png", b"x" * 33)
        assert "32" in str(caught.value)
    finally:
        stack.exec("update wiki.setting set value = '10485760' "
                   "where key = 'max_attachment_bytes'")


def test_the_server_only_writes_the_limit_when_it_is_told_one():
    """An unset variable means "leave it", so an operator who raised the cap
    with an UPDATE does not find it back at ten megabytes after a restart."""
    assert Config.from_env({
        "FSWIKI_DATABASE_URL": "postgres://x",
        "FSWIKI_SCHEMA_DIR": str(ROOT / "server" / "schema"),
    }).max_attachment_bytes is None

    assert Config.from_env({
        "FSWIKI_DATABASE_URL": "postgres://x",
        "FSWIKI_SCHEMA_DIR": str(ROOT / "server" / "schema"),
        "FSWIKI_MAX_ATTACHMENT_BYTES": "1048576",
    }).max_attachment_bytes == 1048576


# --- the mount does not see them -------------------------------------------

async def test_a_file_is_in_the_tree_a_mirror_copies(stack, logo):
    """It is a revision, so there is nothing to leave out. Under the old shape
    it had none, and `syncable_document` had to exclude it or a mirror would
    have written a zero-byte file where a picture was."""
    from fswiki_core.client import Client
    mirror = Client(stack.url, stack.token("alice"))  # the sync tree
    try:
        rows = {row["path"] for row in await mirror.outline()}
        assert "root.public.logo" in rows
        manifest = {row["path"]: row for row in await mirror.manifest()}
        assert manifest["root.public.logo"]["size"] == len(PNG)
        assert manifest["root.public.logo"]["content_type"] == "image/png"
    finally:
        await mirror.aclose()


async def test_and_in_the_one_the_browser_reads(alice, logo):
    paths = {row["path"] for row in await alice.outline()}
    assert "root.public.logo" in paths


async def test_a_file_has_history_like_a_page(alice, stack):
    """The reason it is a revision at all. A separate table could not have
    joined the temporal model, so `document_as_of` would have shown a picture
    that did not exist yet, or the current one at every instant."""
    await alice.attach("root.public.chart", "image/png", PNG)
    try:
        second = await alice.attach("root.public.chart", "image/png", PNG + b"\x00")
        assert second["version"] == 2
        kept = stack.psql(
            "select encode(v.content_bytes, 'hex') from wiki.document_version v "
            "join wiki.document d on d.id = v.document_id "
            "where d.path = 'root.public.chart' and v.version = 1")
        assert bytes.fromhex(kept) == PNG
    finally:
        await alice.detach("root.public.chart")


async def test_retiring_one_keeps_its_history(alice, stack):
    """A tombstone, not a deletion. Attaching it again is another revision
    rather than an apology."""
    await alice.attach("root.public.temp", "image/png", PNG)
    assert await alice.detach("root.public.temp")
    assert await alice.document("root.public.temp") is None
    assert stack.psql("select count(*) from wiki.document_version v "
                      "join wiki.document d on d.id = v.document_id "
                      "where d.path = 'root.public.temp'") == "2"
    again = await alice.attach("root.public.temp", "image/png", PNG)
    assert again["version"] == 3
    await alice.detach("root.public.temp")


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------
#
# Through the real binary, because argument parsing, exit codes and stderr are
# part of what a CLI is. `cli` needs a stack but not a mount, so these stay in
# the half of the suite that runs in a sandbox.

def test_attach_puts_a_file_in_a_folder(cli, tmp_path, stack):
    """A trailing `/` means "under its own name", the way `cp` does it."""
    source = tmp_path / "diagram.png"
    source.write_bytes(PNG)
    r = cli("attach", str(source), "public/", user="alice")
    try:
        assert r.code == 0, r.out
        assert "public/diagram" in r.out
        assert stack.psql("select v.content_type from wiki.document_version v "
                          "join wiki.document d on d.id = v.document_id "
                          "where d.path = 'root.public.diagram' "
                          "and upper_inf(v.valid)") == "image/png"
    finally:
        cli("detach", "public/diagram.png", user="alice")


def test_attach_can_be_told_the_name(cli, tmp_path, stack):
    source = tmp_path / "whatever.png"
    source.write_bytes(PNG)
    r = cli("attach", str(source), "public/plan.png", user="alice")
    try:
        assert r.code == 0, r.out
        assert stack.psql("select count(*) from wiki.document "
                          "where path = 'root.public.plan'") == "1"
    finally:
        cli("detach", "public/plan.png", user="alice")


def test_a_second_attach_says_it_replaced(cli, tmp_path, stack):
    """A path of its own, because a revision number is a property of the path
    and every other test in this file reaches for `public/logo`."""
    source = tmp_path / "repeat.png"
    source.write_bytes(PNG)
    try:
        first = cli("attach", str(source), "public/", user="alice")
        assert "attached" in first.out and "revision 1" in first.out
        source.write_bytes(PNG + b"\x00")
        second = cli("attach", str(source), "public/", user="alice")
        assert "replaced" in second.out and "revision 2" in second.out
    finally:
        cli("detach", "public/repeat.png", user="alice")


def test_an_unguessable_type_asks_rather_than_guessing(cli, tmp_path):
    source = tmp_path / "mystery"
    source.write_bytes(b"?")
    r = cli("attach", str(source), "public/", user="alice")
    assert r.code == 1
    assert "--type" in r.out


def test_the_type_can_be_given(cli, tmp_path, stack):
    source = tmp_path / "mystery"
    source.write_bytes(b"a,b\n1,2\n")
    r = cli("attach", str(source), "public/rows.csv", "--type", "text/csv",
            user="alice")
    try:
        assert r.code == 0, r.out
    finally:
        cli("detach", "public/rows.csv", user="alice")


def test_a_file_that_is_not_there_is_one_clear_error(cli, tmp_path):
    r = cli("attach", str(tmp_path / "nope.png"), "public/", user="alice")
    assert r.code == 1
    assert "cannot read" in r.out


def test_detaching_nothing_says_so_rather_than_succeeding(cli):
    r = cli("detach", "public/not-a-real-file.png", user="alice")
    assert r.code == 1
    assert "nothing to remove" in r.out


def test_detach_says_the_history_is_kept(cli, tmp_path):
    """A file is a revision, so removing one is a retirement. Saying
    "permanently" would now be a lie, and it was the truth two commits ago."""
    source = tmp_path / "gone.png"
    source.write_bytes(PNG)
    cli("attach", str(source), "public/", user="alice")
    r = cli("detach", "public/gone.png", user="alice")
    assert r.code == 0, r.out
    assert "retired" in r.out and "history is kept" in r.out


def test_a_reader_may_not_attach(cli, tmp_path):
    """dave reads the public tree and writes nothing. The refusal is the
    database's -- the CLI has no idea what he may do."""
    source = tmp_path / "sneaky.png"
    source.write_bytes(PNG)
    r = cli("attach", str(source), "public/", user="dave")
    assert r.code == 1
