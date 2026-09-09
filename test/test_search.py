"""Search: what a reader finds, and what search must never tell them.

Three parts.

`excerpt_html` is pure and needs nothing, so it is tested first and hardest.
It is the one place in this project where text from `wiki.search` becomes
markup, and the text it is handed is one person's document being shown to
another. `ts_headline` does not escape, so everything that keeps a `<script>`
out of a results page happens in that function.

The rest runs against the live stack through the real ASGI application,
because the property worth asserting is not "the SQL filters" -- server/test
already proves that over the cross product -- but that nothing between the
database and the browser widens it. A count, a "3 results you cannot see", or
a route that answers differently for a hidden page would each be a disclosure
the SQL suite cannot see from where it stands.

The last part asks the same questions of `fswiki search`, because a second
interface is a second chance to widen something -- and an exit code is a thing
a script branches on, which a web page does not have.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from conftest import ROOT
from fswiki_core.pages import (MARK_CLOSE, MARK_OPEN, excerpt_html,
                               excerpt_parts, fold_drafts)
from fswiki_server.app import Application
from fswiki_server.config import Config

pytestmark = pytest.mark.anyio

SEARCH = "/-/search"


def mark(text: str) -> str:
    """What `wiki.search` would have returned, written readably."""
    return text.replace("[", MARK_OPEN).replace("]", MARK_CLOSE)


# ---------------------------------------------------------------------------
# The excerpt is the only place document text becomes markup
# ---------------------------------------------------------------------------

def test_a_match_becomes_a_mark():
    assert excerpt_html(mark("a [word] here")) == "a <mark>word</mark> here"


def test_nothing_else_becomes_anything():
    assert excerpt_html("plain text") == "plain text"


def test_the_document_is_escaped_and_the_markers_are_not():
    assert excerpt_html(mark("<b>[x]</b>")) == "&lt;b&gt;<mark>x</mark>&lt;/b&gt;"


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "</mark><script>x</script>",
    "<style>body{display:none}</style>",
    "javascript:alert(1)",
    "<iframe src='//evil'></iframe>",
])
def test_no_tag_a_document_contains_survives(hostile):
    """The whole argument for the control characters. If the markers had been
    `<b>` and `</b>` there would be no way to escape the text around them
    without also escaping them."""
    out = excerpt_html(hostile)
    assert "<" not in out.replace("<mark>", "").replace("</mark>", "")


def test_a_marker_a_document_contains_cannot_bleed():
    """An author who types a STX opens a highlight. It has to close by the end
    of their own excerpt rather than tinting everything after it."""
    out = excerpt_html(MARK_OPEN + "everything after this")
    assert out == "<mark>everything after this</mark>"
    assert out.count("<mark>") == out.count("</mark>")


def test_a_stray_close_is_dropped_rather_than_emitted():
    out = excerpt_html("nothing was open" + MARK_CLOSE)
    assert out == "nothing was open"


@pytest.mark.parametrize("text", [
    "", MARK_OPEN, MARK_CLOSE, MARK_OPEN * 5, MARK_CLOSE * 5,
    MARK_OPEN + MARK_CLOSE + MARK_OPEN, mark("[a][b][c]"),
    mark("[nested [inner] outer]"),
])
def test_every_shape_of_marker_comes_out_balanced(text):
    out = excerpt_html(text)
    assert out.count("<mark>") == out.count("</mark>")


def test_none_is_not_a_crash():
    assert excerpt_html(None) == ""


# ---------------------------------------------------------------------------
# The runs underneath it, which a terminal renders instead
# ---------------------------------------------------------------------------

def test_the_runs_carry_the_text_and_whether_it_matched():
    assert excerpt_parts(mark("a [word] here")) == [
        ("a ", False), ("word", True), (" here", False)]


def test_adjacent_runs_of_the_same_kind_are_one_run():
    """So no caller has to merge them, and none can forget to."""
    assert excerpt_parts(mark("[a][b]")) == [("ab", True)]


def test_a_stray_open_closes_at_the_end_of_the_excerpt():
    assert excerpt_parts(MARK_OPEN + "rest") == [("rest", True)]


def test_a_stray_close_marks_nothing():
    assert excerpt_parts("plain" + MARK_CLOSE) == [("plain", False)]


def test_markers_are_never_text():
    """Whatever the shape, no run may contain one. A terminal does not escape,
    so a marker that survived would be written to somebody's screen."""
    for text in ["", MARK_OPEN, MARK_CLOSE, MARK_OPEN * 5, MARK_CLOSE * 5,
                 mark("[nested [inner] outer]"), mark("[a][b][c]")]:
        for run, _ in excerpt_parts(text):
            assert MARK_OPEN not in run and MARK_CLOSE not in run


def test_the_html_is_built_from_the_same_runs():
    """Not an implementation detail: it is what stops a terminal and a browser
    disagreeing about a hostile excerpt."""
    hostile = mark("<b>[x]</b>")
    assert excerpt_html(hostile) == "&lt;b&gt;<mark>x</mark>&lt;/b&gt;"
    assert [run for run, _ in excerpt_parts(hostile)] == ["<b>", "x", "</b>"]


# ---------------------------------------------------------------------------
# Folding drafts in, which the preview and the CLI both do
# ---------------------------------------------------------------------------

def hit(path, rank=0.5, **extra):
    return {"path": path, "rank": rank, **extra}


def test_published_results_are_marked_as_not_drafts():
    assert fold_drafts([hit("root.a")], []) == [
        {"path": "root.a", "rank": 0.5, "state": None}]


def test_a_draft_of_a_published_page_replaces_it():
    """A draft is what its author sees in the mount, in `status` and on the
    page. Search showing them the published text instead would be search
    being wrong."""
    rows = fold_drafts([hit("root.a", 0.9)], [hit("root.a", 0.1)])
    assert rows == [{"path": "root.a", "rank": 0.1, "state": "draft"}]


def test_a_draft_of_a_page_that_is_not_published_is_still_a_result():
    rows = fold_drafts([hit("root.a")], [hit("root.b")])
    assert {row["path"] for row in rows} == {"root.a", "root.b"}


def test_results_are_ranked_and_ties_break_on_path():
    rows = fold_drafts([hit("root.z", 0.9), hit("root.b", 0.1),
                        hit("root.a", 0.1)], [])
    assert [row["path"] for row in rows] == ["root.z", "root.a", "root.b"]


def test_a_row_with_no_rank_sorts_last_rather_than_crashing():
    rows = fold_drafts([hit("root.a", None), hit("root.b", 0.1)], [])
    assert [row["path"] for row in rows] == ["root.b", "root.a"]


def test_the_limit_applies_after_the_fold():
    """Both halves come back with `limit` rows each, so the union is over it."""
    rows = fold_drafts([hit(f"root.p{i}", 0.9) for i in range(5)],
                       [hit(f"root.d{i}", 0.9) for i in range(5)], limit=3)
    assert len(rows) == 3


def test_the_inputs_are_not_modified():
    published = [hit("root.a")]
    fold_drafts(published, [])
    assert "state" not in published[0]


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
def quokkas(stack):
    """A page of our own, holding a word nothing else in the suite writes.

    The shared fixtures are shared: another test rewriting
    `engineering/onboarding` changes what its excerpt says, and an assertion
    about a highlight then fails for a reason that has nothing to do with
    search. A page nobody else touches is the only way to assert on an excerpt
    and mean it.
    """
    stack.exec("""
        insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
        select d.id, 'quokkas', false, 'Quokkas',
               (select p.id from wiki.principal p where p.name = 'alice')
          from wiki.document d where d.path = 'root.public'::ltree;
        insert into wiki.document_version
               (document_id, version, path, content, message, author_id)
        select d.id, 1, d.path,
               'The quokkas are a small marsupial, and this page is about them.',
               'initial',
               (select p.id from wiki.principal p where p.name = 'alice')
          from wiki.document d where d.path = 'root.public.quokkas'::ltree;
    """)
    yield "public/quokkas"
    stack.exec("""
        delete from wiki.document_version v using wiki.document d
         where v.document_id = d.id and d.path = 'root.public.quokkas'::ltree;
        delete from wiki.document where path = 'root.public.quokkas'::ltree;
    """)


def find(browser, terms, *, who=None, stack=None):
    headers = ({"Authorization": f"Bearer {stack.token(who)}"} if who else {})
    return browser.get(SEARCH, params={"q": terms}, headers=headers)


def results(page: str) -> str:
    """The page below the search box.

    The box echoes the query back, so comparing whole pages compares the words
    that were typed rather than what the wiki answered. Everything these
    assertions are about lives after the form.
    """
    return page.rsplit("</form>", 1)[-1]


# --- it works ---------------------------------------------------------------

async def test_a_reader_finds_a_page_they_may_read(browser, stack):
    r = await find(browser, "onboarding", who="bob", stack=stack)
    assert r.status_code == 200
    assert "engineering/onboarding" in results(r.text)


async def test_the_result_links_where_the_page_is(browser, stack):
    r = await find(browser, "onboarding", who="bob", stack=stack)
    assert 'href="/engineering/onboarding"' in results(r.text)


async def test_the_excerpt_is_there_and_is_highlighted(browser, stack, quokkas):
    r = await find(browser, "quokkas", who="bob", stack=stack)
    body = results(r.text)
    assert "<mark>quokkas</mark>" in body
    assert "small marsupial" in body


async def test_the_excerpt_is_content_and_not_the_whole_page(browser, stack, quokkas):
    """A results list is a list of reasons to click, not a copy of the wiki."""
    r = await find(browser, "marsupial", who="bob", stack=stack)
    assert "<h1" not in results(r.text)


async def test_two_matches_both_come_back_ranked(browser, stack):
    """`permissions` is in the handbook index and in the page it links to."""
    r = await find(browser, "permissions", who="bob", stack=stack)
    body = results(r.text)
    assert "public/guide/permissions" in body and "public/guide/index" in body


async def test_the_box_is_on_every_page_not_just_this_one(browser, stack):
    for route in ("/", "/engineering/onboarding", SEARCH):
        r = await browser.get(route, headers={
            "Authorization": f"Bearer {stack.token('bob')}"})
        assert "class=find" in r.text, route


# --- and it does not work for anyone else -----------------------------------

async def test_a_page_you_may_not_read_is_not_in_your_results(browser, stack):
    """`root.locked` holds the only `locked` in the fixtures, and alice is the
    only one who may read it."""
    r = await find(browser, "locked", who="bob", stack=stack)
    assert r.status_code == 200
    assert "locked" not in results(r.text)


async def test_not_even_for_the_person_who_owns_it(browser, stack):
    """The sharpest case in the fixtures. Dave owns `root.locked` and is denied
    everything on it, so ownership must not be a way back in through a route
    that was written later than the ACL.
    """
    r = await find(browser, "locked", who="dave", stack=stack)
    assert "locked" not in results(r.text)


async def test_but_its_reader_finds_it(browser, stack):
    """The other half. Without it the two tests above would pass against a
    search that is simply broken."""
    r = await find(browser, "locked", who="alice", stack=stack)
    assert 'href="/locked"' in results(r.text)


async def test_a_deny_reaches_a_whole_subtree(browser, stack):
    """Carol reads `engineering/onboarding` and nothing else under
    `engineering/`, so the secret plans beside it are not hers to find."""
    mine = await find(browser, "onboarding", who="carol", stack=stack)
    theirs = await find(browser, "secret", who="carol", stack=stack)
    assert "engineering/onboarding" in results(mine.text)
    assert "secret" not in results(theirs.text)


async def test_a_refused_word_answers_exactly_like_an_unwritten_one(browser, stack):
    """The disclosure this route could make, and the reason the answer is a
    sentence rather than a count.

    "Nothing matches" for a word nobody wrote and "nothing matches" for a word
    that is only in a page you may not open have to be the same answer --
    otherwise the search box is an oracle over the whole wiki, one word at a
    time. Compared below the box, because the box echoes the two different
    words back.
    """
    refused = await find(browser, "locked", who="bob", stack=stack)
    nonsense = await find(browser, "zzzunwrittenword", who="bob", stack=stack)
    assert results(refused.text) == results(nonsense.text)


async def test_an_anonymous_visitor_finds_nothing_they_were_not_granted(browser):
    """Nothing in the fixtures is granted to `public`, so the honest answer for
    an anonymous caller is the empty one -- for a word that is certainly in the
    wiki as much as for one that is not."""
    present = await browser.get(SEARCH, params={"q": "onboarding"})
    absent = await browser.get(SEARCH, params={"q": "zzzunwrittenword"})
    assert present.status_code == 200
    assert results(present.text) == results(absent.text)


# --- the shape of the route -------------------------------------------------

async def test_no_query_is_the_box_and_nothing_else(browser, stack):
    r = await find(browser, "", who="bob", stack=stack)
    assert r.status_code == 200
    assert "class=find" in r.text
    assert "class=results" not in r.text


async def test_a_query_of_only_spaces_is_the_same(browser, stack):
    r = await find(browser, "     ", who="bob", stack=stack)
    assert "class=results" not in r.text


async def test_the_terms_come_back_in_the_box(browser, stack):
    r = await find(browser, "onboarding", who="bob", stack=stack)
    assert 'value="onboarding"' in r.text


async def test_the_terms_are_escaped_on_the_way_back(browser, stack):
    """The query is the one piece of attacker-controlled text on this page that
    did not come from the database, and it is echoed into an attribute."""
    r = await find(browser, '"><script>alert(1)</script>', who="bob", stack=stack)
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


async def test_it_is_still_read_only(browser):
    r = await browser.post(SEARCH)
    assert r.status_code == 405


async def test_the_reserved_prefix_still_refuses_what_it_does_not_serve(browser):
    """`/-/` is reserved, which is what stops a page called `search` from
    shadowing this route."""
    r = await browser.get("/-/nothing-of-the-sort")
    assert r.status_code == 404


async def test_a_very_long_query_is_answered_rather_than_refused(browser, stack):
    r = await find(browser, "word " * 500, who="bob", stack=stack)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# The same questions, asked from a terminal
# ---------------------------------------------------------------------------
#
# The CLI reaches `wiki.search` through the same client the browser server
# uses, so the SQL is not on trial again here. What is on trial is that the
# second interface did not widen anything -- an exit code, a count or a wording
# that differs between "no such word" and "a word you may not read" would be
# the same disclosure the route above is careful about, arriving by a door
# nobody was watching.
#
# Every one takes `clean`, because `search` folds this author's own drafts in.
# A draft another test file left behind would be a result here, and the two
# runs the oracle test compares would stop being comparable.

def test_the_cli_finds_a_page_its_reader_may_read(cli, clean):
    r = cli("search", "onboarding")
    assert r.code == 0 and "engineering/onboarding" in r


def test_the_cli_shows_the_excerpt_with_no_markers_in_it(cli, quokkas, clean):
    """STX and ETX reaching a terminal would be written to somebody's screen
    verbatim. `excerpt_line` is what stops that."""
    r = cli("search", "quokkas")
    assert "small marsupial" in r
    assert MARK_OPEN not in r.out and MARK_CLOSE not in r.out


def test_the_cli_takes_several_words_as_one_query(cli, quokkas, clean):
    r = cli("search", "small", "marsupial")
    assert r.code == 0 and "public/quokkas" in r


def test_a_quoted_query_can_exclude_a_word(cli, quokkas, clean):
    """`websearch_to_tsquery` reads `-word` as an exclusion. It has to arrive
    as one argument: argparse reads a bare `-marsupial` as an option and
    refuses it, which is why the help says to quote the query."""
    kept = cli("search", "quokkas")
    dropped = cli("search", "quokkas -marsupial")
    assert "public/quokkas" in kept
    assert "public/quokkas" not in dropped.out


def test_the_cli_finds_nothing_and_that_is_not_a_failure(cli, clean):
    """An empty result is an answer. A non-zero exit would put it in the same
    class as an unreachable server, which is what a script would branch on."""
    r = cli("search", "zzzunwrittenword")
    assert r.code == 0
    assert "Nothing here matches that." in r


def test_the_cli_refuses_a_page_this_reader_may_not_read(cli, clean):
    r = cli("search", "locked", user="bob")
    assert "locked" not in r.out


def test_but_its_reader_finds_it_from_the_terminal_too(cli, clean):
    """The other half of the two above. Without it they would both pass
    against a `search` that simply returns nothing."""
    r = cli("search", "locked", user="alice")
    assert r.code == 0 and "1 result" in r and "locked" in r


def test_the_cli_answers_a_refused_word_exactly_like_an_unwritten_one(cli, clean):
    """The oracle test again, one interface along. The terms are echoed in the
    heading of a result list, and there is no result list here, so the two
    outputs are comparable in full."""
    refused = cli("search", "locked", user="bob")
    nonsense = cli("search", "zzzunwrittenword", user="bob")
    assert refused.out == nonsense.out
    assert refused.code == nonsense.code
