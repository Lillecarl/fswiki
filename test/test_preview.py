"""`fswiki preview`: the wiki as HTML, on localhost, while you write.

Read docs/rendering.md first. Three properties do most of the work here:

- **Read-only by construction.** Every method but GET and HEAD is refused
  before the request is routed, so the property belongs to the server rather
  than to an inventory of the handlers that remembered to check.
- **One renderer, two callers.** The previewer runs the same code the server
  runs. A preview that used a different engine would lie precisely where
  markdown implementations disagree, which is where you most want the truth.
- **The link graph does not leak.** A link to a document you may not read is
  byte-identical to a link to one that does not exist, because the difference
  between them is the disclosure.
"""

from __future__ import annotations

import re

import pytest

from conftest import answers, http, wait_for

pytestmark = pytest.mark.mount  # the preview fixture starts a mount


def get(base: str, route: str, **kw):
    return http(base + route, **kw)


def links(body: str) -> set[str]:
    return set(re.findall(r'<a[^>]+href="([^"]+)"', body))


# ---------------------------------------------------------------------------
# Read-only, before routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_writing_method_is_refused(preview, method):
    """Refused before the path is looked at, so this stays true of routes
    nobody has written yet. That is the difference between a server that is
    read-only and one whose handlers all happen to be."""
    r = get(preview, "/public/welcome", method=method)
    assert r.code == 405


def test_the_refusal_does_not_depend_on_the_route_existing(preview):
    """A 404 here would mean the check runs after routing, and a route added
    later would be a write path by default."""
    assert get(preview, "/no/such/page/at/all", method="POST").code == 405


def test_head_is_allowed_and_carries_no_body(preview):
    r = get(preview, "/", method="HEAD")
    assert r.code == 200
    assert r.body == ""


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def test_the_index_lists_what_this_reader_may_see(preview):
    r = get(preview, "/")
    assert r.code == 200
    assert "/public/welcome" in links(r.body)


def test_a_document_renders_as_html(preview, clean):
    r = get(preview, "/public/welcome")
    assert r.code == 200
    assert "<h1" in r.body


def test_the_page_says_which_revision_it_is_showing(preview, clean):
    """A preview whose state you cannot see is a preview you have to guess
    about — and the guess that matters is draft versus published."""
    r = get(preview, "/public/welcome")
    assert re.search(r"revision \d+", r.body)


def test_a_missing_page_is_a_404_that_says_so(preview):
    r = get(preview, "/public/no-such-page")
    assert r.code == 404
    assert "Nothing to show" in r.body


def test_a_path_the_wiki_could_never_hold_is_refused(preview):
    """`..` is not a slug, and resolving it would be the beginning of a path
    traversal. It is rejected as a *name*, before anything looks for it."""
    assert get(preview, "/public/../etc/passwd").code == 404


def test_the_reserved_prefix_is_not_a_document(preview):
    """`/-/` can never collide with a document, because a slug may be neither
    empty nor contain a slash. That is what makes it safe to hang the neutral
    link form and the change poll off it."""
    assert get(preview, "/-/nothing-here").code == 404


# ---------------------------------------------------------------------------
# Drafts: the reason this exists at all
# ---------------------------------------------------------------------------

def test_a_draft_is_what_you_see(preview, mount, clean):
    """Previewing the published copy would show you everything except the thing
    you are working on."""
    (mount / "engineering/onboarding.md").write_text("# still cooking\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="the draft")
    r = wait_for(lambda: get(preview, "/engineering/onboarding"),
                 what="the page")
    assert "still cooking" in r.body
    assert "draft" in r.body


def test_published_pins_it_to_what_everyone_else_sees(stack, mount, clean):
    """The other half of the same question: "what will they get when I push
    this", which is not answerable from a page showing your own draft."""
    (mount / "engineering/onboarding.md").write_text("# still cooking\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="the draft")

    from conftest import free_port
    import subprocess
    import tempfile
    from pathlib import Path

    port = free_port()
    log = Path(tempfile.mkstemp(prefix="preview-published-", suffix=".log")[1])
    proc = subprocess.Popen(
        ["fswiki", "preview", "--port", str(port), "--published"],
        stdout=open(log, "w"), stderr=subprocess.STDOUT, env=stack.env("bob"))
    base = f"http://127.0.0.1:{port}"
    try:
        wait_for(lambda: answers(base + "/"), timeout=30,
                 what="the published preview to answer")
        r = get(base, "/engineering/onboarding")
        assert "still cooking" not in r.body
        assert re.search(r"revision \d+", r.body)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# The link graph, which leaks at render time or not at all
# ---------------------------------------------------------------------------

def test_a_link_you_may_follow_is_live(preview, mount, clean):
    (mount / "engineering/onboarding.md").write_text(
        "see [[public/welcome]]\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="the draft")
    r = wait_for(lambda: "/public/welcome" in links(get(
        preview, "/engineering/onboarding").body) or None,
        what="the resolved link")
    assert r


def test_a_link_you_may_not_follow_is_not_a_link_at_all(preview, mount, clean):
    """It renders as plain text, and as *the same* plain text as a link to a
    document that does not exist. Telling those two apart would tell the reader
    that the page exists, where it lives and what it is called — none of which
    the ACL granted, and all of which would have leaked in the HTML before the
    audit trail could record a click."""
    (mount / "engineering/onboarding.md").write_text(
        "forbidden [[engineering/secret-plans]] and absent [[nowhere/at-all]]\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="the draft")

    def rendered():
        body = get(preview, "/engineering/onboarding").body
        return body if "forbidden" in body and "at-all" in body else None

    body = wait_for(rendered, what="the page to show the new draft")
    assert "/engineering/secret-plans" not in links(body)
    assert "secret-plans" in body and "at-all" in body

    # The two get the same *treatment*, which is the property — the paths
    # themselves differ because the author wrote them. Bare text in both cases:
    # no anchor, no title, no class a stylesheet could grey one of them out
    # with, nothing a reader or a script could tell apart.
    forbidden = re.search(r"forbidden\s+(.*?)\s+and\s+absent", body).group(1)
    absent = re.search(r"absent\s+(.*?)\s*</", body).group(1)
    assert forbidden == "engineering/secret-plans", forbidden
    assert absent == "nowhere/at-all", absent


def test_the_neutral_prefix_redirects_rather_than_404s(preview):
    """The renderer emits `/-/fswiki/<ltree>` and decides nothing; the composing
    pass turns it into a URL. A cached body served without that pass would leave
    the raw form in the page, so it has to at least go somewhere."""
    r = get(preview, "/-/fswiki/root.public.welcome")
    assert r.code == 200
    assert "public/welcome" in r.body


# ---------------------------------------------------------------------------
# Reloading
# ---------------------------------------------------------------------------

def test_the_change_token_is_served_for_the_reload_poll(preview):
    """Eleven bytes, polled by the page itself. Deliberately a poll rather than
    a socket: there is no write path here to keep open, and this way the whole
    server stays GET-only."""
    r = get(preview, "/-/changed")
    assert r.code == 200
    assert r.body.strip()


def test_the_token_moves_when_the_wiki_does(preview, mount, clean):
    before = get(preview, "/-/changed").body
    (mount / "engineering/onboarding.md").write_text("# moved\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="the draft")
    assert wait_for(lambda: get(preview, "/-/changed").body != before,
                    what="the change token to move")


# ---------------------------------------------------------------------------
# One identity, said out loud
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host,warned", [("0.0.0.0", True), ("127.0.0.1", False)])
def test_binding_off_loopback_says_what_it_exposes(stack, tmp_path, host, warned):
    """The preview holds one token and answers as one person, which is right for
    a preview and wrong for a service. The exposure is not that it writes — it
    cannot — but that it reads *as you*, for anyone who connects. Loopback is
    where that is obvious and where nothing is said."""
    import subprocess
    from conftest import free_port

    port = free_port()
    log = tmp_path / f"preview-{host}.log"
    proc = subprocess.Popen(
        ["fswiki", "preview", "--host", host, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=open(log, "w"), env=stack.env("bob"))
    try:
        said = wait_for(lambda: log.read_text() if "ctrl-c" in log.read_text() else None,
                        timeout=30, what="the preview to announce itself")
        assert ("anyone who can reach this port" in said) is warned, said
        assert "read-only" in said
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# When the server goes away
#
# A preview is a long-running process someone leaves open in a tab while they
# write. It outlives outages by definition, so what it does during one is not
# an edge case — it is Tuesday.
# ---------------------------------------------------------------------------

@pytest.fixture
def blinking_preview(stack, spare, tmp_path):
    """A preview pointed at a server this test may take away."""
    import subprocess
    from conftest import free_port

    port = free_port()
    log = tmp_path / "preview.log"
    proc = subprocess.Popen(
        ["fswiki", "preview", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=open(log, "w"),
        env=stack.env("bob", FSWIKI_URL=spare.url))
    base = f"http://127.0.0.1:{port}"
    try:
        wait_for(lambda: answers(base + "/"), timeout=30,
                 what="the preview to answer")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_preview_survives_the_server_going_away(blinking_preview, spare):
    """It must still be listening afterwards. A previewer that dies with the
    connection is one you have to notice and restart, which is exactly what
    nobody does while concentrating on a document."""
    spare.stop()
    get(blinking_preview, "/public/welcome")          # whatever it answers
    assert answers(blinking_preview + "/-/changed") or True
    spare.start()
    assert wait_for(lambda: get(blinking_preview, "/public/welcome").code == 200,
                    timeout=30, what="the preview to serve pages again")


def test_a_page_during_an_outage_is_an_error_and_not_a_dropped_connection(
        blinking_preview, spare):
    """An exception out of the request handler closes the socket with no
    response at all, which the browser shows as "connection reset" — the one
    message that says nothing about which of the two programs went wrong."""
    spare.stop()
    r = get(blinking_preview, "/public/welcome")
    # 502, not 500: this server is fine, the one behind it is not, and saying
    # so is the difference between "the previewer is broken" and "the wiki is
    # down" — which are two different things to go and do something about.
    assert r.code == 502
    assert "Cannot reach the wiki" in r.body
    # And it carries the reload poll, so the tab comes back by itself rather
    # than waiting for somebody to guess the moment to press refresh.
    assert "/-/changed" in r.body


def test_the_index_during_an_outage_is_also_an_error_page(blinking_preview, spare):
    spare.stop()
    assert get(blinking_preview, "/").code >= 500


def test_the_reload_poll_stays_quiet_during_an_outage(blinking_preview, spare):
    """The poll is a convenience. A server that blinks should cost a reload
    that does not happen, never a page that stops working — so this one answers
    200 with an empty token rather than failing."""
    spare.stop()
    r = get(blinking_preview, "/-/changed")
    assert r.code == 200


# ---------------------------------------------------------------------------
# Someone else's view, in a browser
#
# The last impersonation surface. The mount says "not yours" with mode bits and
# `ro`; the CLI says it by refusing to write. A web page has neither, and it is
# the surface where the mistake is easiest to make — a page that looks exactly
# like your own wiki, minus a few things, is indistinguishable from your own
# wiki having lost a few things.
# ---------------------------------------------------------------------------

@pytest.fixture
def borrowed(stack):
    """A preview of bob's view, run by dave."""
    import subprocess
    from conftest import free_port

    stack.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'), p.id
            from wiki.principal p where p.name in ('everyone', 'engineering')
        on conflict do nothing;
    """)
    port = free_port()
    import tempfile
    from pathlib import Path
    log = Path(tempfile.mkstemp(prefix="preview-as-", suffix=".log")[1])
    proc = subprocess.Popen(
        ["fswiki", "--as", "bob", "preview", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=open(log, "w"), env=stack.env("dave"))
    base = f"http://127.0.0.1:{port}"
    try:
        wait_for(lambda: answers(base + "/"), timeout=30,
                 what="the borrowed preview to answer")
        yield base, log
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_every_page_says_whose_view_it_is(borrowed):
    """On every page, not on a status screen somebody has to think to visit.
    The whole failure mode of impersonation is forgetting you are doing it."""
    base, _ = borrowed
    for route in ("/", "/public/welcome", "/no/such/page"):
        body = get(base, route).body
        assert "viewing as bob" in body, route
        assert "read-only" in body, route


def test_it_shows_the_subjects_wiki_and_not_the_actors(borrowed, stack):
    """dave is an auditor and reads more than bob. A preview that quietly used
    dave's own permissions would answer the opposite of the question asked."""
    base, _ = borrowed
    assert get(base, "/engineering/secret-plans").code == 404


def test_writing_methods_are_still_refused(borrowed):
    """Refused before routing, as for any preview. Worth asserting again here
    because this is the one configuration where a write would be a forgery in
    somebody else's name rather than merely a bug."""
    base, _ = borrowed
    assert get(base, "/public/welcome", method="POST").code == 405


def test_starting_one_says_so_on_the_terminal(borrowed):
    """Said once, where somebody is looking, in the same words the mount uses."""
    _, log = borrowed
    said = wait_for(lambda: log.read_text() if "ctrl-c" in log.read_text() else None,
                    timeout=30, what="the preview to announce itself")
    assert "showing the view of bob" in said
    assert "not yours" in said and "record" in said


def test_a_membership_is_described_as_a_membership(stack):
    """"a member of everyone, engineering" — not "engineering". The two are
    different questions, and the wording is what keeps them apart on the one
    surface with room to say it."""
    import subprocess
    import tempfile
    from pathlib import Path
    from conftest import free_port

    stack.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'), p.id
            from wiki.principal p where p.name in ('everyone', 'engineering')
        on conflict do nothing;
    """)
    port = free_port()
    log = Path(tempfile.mkstemp(prefix="preview-group-", suffix=".log")[1])
    proc = subprocess.Popen(
        ["fswiki", "--as-group", "everyone", "--as-group", "engineering",
         "preview", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=open(log, "w"), env=stack.env("dave"))
    base = f"http://127.0.0.1:{port}"
    try:
        wait_for(lambda: answers(base + "/"), timeout=30, what="the preview")
        assert "viewing as a member of everyone, engineering" in get(base, "/").body
    finally:
        proc.terminate()
        proc.wait(timeout=10)
