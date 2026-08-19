"""The render cache, and the two things that make it correct rather than fast.

A revision's content never changes, so `(document_id, version, renderer)` names
one byte string forever. There is nothing to invalidate, only something to
evict -- which is why this is a small file about eviction and a careful one
about what is allowed in at all.

The two properties that matter are not performance properties:

* what is stored is the **neutral** body, before wiki links are resolved
  against a particular reader. Cache the composed page and one reader's link
  graph reaches another.
* a **draft** is never stored, because its content is mutable and it has no
  version, so the key that makes any of this safe does not exist for it.

No stack and no network: `Pages` is driven by a stub client, so these run at
unit-test speed.
"""

from __future__ import annotations

import pytest

from fswiki_core import render
from fswiki_core.pages import Pages
from fswiki_core.render import cache as render_cache
from fswiki_core.render.cache import Cache, Key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def key(document_id="doc-1", version=1, renderer="engine/1+fswiki1") -> Key:
    return Key(document_id, version, renderer)


# --- the container ----------------------------------------------------------

def test_a_stored_body_comes_back():
    c = Cache()
    c.put(key(), "<p>hi</p>")
    assert c.get(key()) == "<p>hi</p>"
    assert (c.hits, c.misses) == (1, 0)


def test_an_absent_key_is_a_miss_and_not_an_error():
    c = Cache()
    assert c.get(key()) is None
    assert (c.hits, c.misses) == (0, 1)


@pytest.mark.parametrize("other", [
    key(document_id="doc-2"),
    key(version=2),
    key(renderer="engine/2+fswiki1"),
    key(renderer="engine/1+fswiki2"),
])
def test_every_part_of_the_key_is_part_of_the_key(other):
    """Drop any one of the three and the cache serves bytes the running code
    would not produce. The renderer is the one that gets forgotten, and it is
    the one whose absence is silent."""
    c = Cache()
    c.put(key(), "<p>original</p>")
    assert c.get(other) is None


def test_storing_the_same_key_twice_does_not_double_the_bytes():
    c = Cache()
    c.put(key(), "<p>aaaa</p>")
    c.put(key(), "<p>bb</p>")
    assert len(c) == 1
    assert c.nbytes == len(b"<p>bb</p>")
    assert c.get(key()) == "<p>bb</p>"


def test_it_counts_bytes_and_not_entries():
    c = Cache()
    c.put(key(document_id="a"), "x" * 100)
    c.put(key(document_id="b"), "y" * 250)
    assert (len(c), c.nbytes) == (2, 350)


# --- eviction ---------------------------------------------------------------

def test_eviction_actually_evicts():
    c = Cache(max_bytes=300)
    c.put(key(document_id="a"), "a" * 200)
    c.put(key(document_id="b"), "b" * 200)
    assert c.get(key(document_id="a")) is None
    assert c.get(key(document_id="b")) == "b" * 200
    assert c.evictions == 1
    assert c.nbytes <= c.max_bytes


def test_it_evicts_the_least_recently_used_and_not_the_oldest():
    """The distinction is the whole of LRU. `a` is stored first and read last,
    so the one to drop is `b`."""
    c = Cache(max_bytes=250)
    c.put(key(document_id="a"), "a" * 100)
    c.put(key(document_id="b"), "b" * 100)
    assert c.get(key(document_id="a")) == "a" * 100     # a is now the newest
    c.put(key(document_id="c"), "c" * 100)
    assert c.get(key(document_id="b")) is None
    assert c.get(key(document_id="a")) == "a" * 100


def test_a_body_too_large_to_store_is_refused_rather_than_flushing_everything():
    """Storing it would evict the whole cache and then itself, which is a cache
    that holds one page and misses on all of them."""
    c = Cache(max_bytes=100)
    c.put(key(document_id="small"), "s" * 50)
    c.put(key(document_id="huge"), "h" * 5000)
    assert c.get(key(document_id="small")) == "s" * 50
    assert c.get(key(document_id="huge")) is None
    assert c.oversized == 1
    assert c.evictions == 0


def test_the_bound_holds_however_much_is_put_in():
    c = Cache(max_bytes=1000)
    for n in range(200):
        c.put(key(document_id=f"doc-{n}"), "x" * 100)
    assert c.nbytes <= 1000
    assert len(c) <= 10


def test_clear_empties_it_including_the_byte_count():
    c = Cache()
    c.put(key(), "x" * 100)
    c.clear()
    assert (len(c), c.nbytes) == (0, 0)
    assert c.get(key()) is None


def test_the_stats_name_what_an_operator_needs():
    c = Cache(max_bytes=64)
    c.put(key(document_id="a"), "a" * 40)
    c.put(key(document_id="b"), "b" * 40)
    c.get(key(document_id="b"))
    c.get(key(document_id="a"))
    stats = c.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["evictions"] == 1
    assert stats["bytes"] <= stats["max_bytes"] == 64


# --- the key, built from a real pipeline ------------------------------------

def test_renderer_id_matches_what_render_stamps_on_the_output():
    """The key is built before there is anything to render, so the two ways of
    getting it have to agree. If they drift, every request is a miss and the
    cache silently becomes a memory leak."""
    assert render.renderer_id() == render.render("x").renderer


def test_renderer_id_refuses_an_unknown_backend_the_way_render_does():
    with pytest.raises(render.UnknownBackend):
        render.renderer_id(backend="nothing-by-that-name")


# --- through Pages, which is what decides whether a body may be cached ------

class StubClient:
    """Enough of `Client` for `Pages.page`, and a count of the reads it made."""

    base_url = "http://stub"

    def __init__(self, *, document=None, drafts=()):
        self._document = document
        self._drafts = list(drafts)
        self.reads = 0

    async def outline(self):
        return [{"path": "root.welcome", "is_folder": False}]

    async def drafts(self):
        return self._drafts

    async def document(self, path):
        self.reads += 1
        return self._document


PUBLISHED = {"id": "doc-1", "path": "root.welcome", "version": 3,
             "content": "# Hello\n\nsome *text*", "content_type": "text/markdown"}


async def test_a_second_read_of_the_same_revision_is_a_hit():
    c = Cache()
    client = StubClient(document=PUBLISHED)
    pages = Pages(client, drafts=False, cache=c)

    first = await pages.page("root.welcome")
    second = await pages.page("root.welcome")

    assert first == second
    assert (c.hits, c.misses) == (1, 1)
    # The document is still fetched every time. The cache saves the render,
    # not the round trip -- the ACL decision has to happen per request.
    assert client.reads == 2


async def test_a_new_revision_misses():
    c = Cache()
    client = StubClient(document=PUBLISHED)
    pages = Pages(client, drafts=False, cache=c)
    await pages.page("root.welcome")

    client._document = {**PUBLISHED, "version": 4, "content": "# Hello again"}
    status, html = await pages.page("root.welcome")

    assert "Hello again" in html
    assert c.misses == 2


async def test_a_draft_is_never_cached():
    """A draft's content is mutable and it has no version, so there is no key
    that can name it. Not caching it is the point, and the assertion is that
    the second read shows the *changed* draft rather than the first one."""
    c = Cache()
    draft = {"path": "root.welcome", "content": "# One",
             "content_type": "text/markdown"}
    client = StubClient(document=PUBLISHED, drafts=[draft])
    pages = Pages(client, drafts=True, cache=c)

    await pages.page("root.welcome")
    draft["content"] = "# Two"
    _, html = await pages.page("root.welcome")

    # On the rendered heading, not on the word: the stylesheet in the shell
    # has "One" in a comment, and searching for the word finds that instead.
    assert "<h1>Two</h1>" in html and "<h1>One</h1>" not in html
    assert len(c) == 0
    assert (c.hits, c.misses) == (0, 0)


async def test_what_is_stored_is_the_neutral_body_and_not_the_composed_page():
    """The security property. Links are resolved per reader *after* this, so a
    stored body must still carry the reserved prefix -- otherwise one reader's
    link graph is served to the next."""
    c = Cache()
    client = StubClient(document={**PUBLISHED,
                                  "content": "see [[welcome]] and [[secret]]"})
    pages = Pages(client, drafts=False, cache=c)
    await pages.page("root.welcome")

    stored = next(iter(c._entries.values()))[0]
    assert render.links.PREFIX in stored
    assert render.links.unresolved(stored) == 2


async def test_two_readers_of_one_revision_get_their_own_link_graphs():
    """The same cached bytes, composed differently. `root.secret` resolves for
    the reader who can see it and is refused for the one who cannot, from one
    stored body."""
    c = Cache()
    body = {**PUBLISHED, "content": "see [[secret]]"}

    class Reader(StubClient):
        def __init__(self, paths):
            super().__init__(document=body)
            self._paths = paths

        async def outline(self):
            return [{"path": p, "is_folder": False} for p in self._paths]

    _, allowed = await Pages(Reader(["root.welcome", "root.secret"]),
                             drafts=False, cache=c).page("root.welcome")
    _, refused = await Pages(Reader(["root.welcome"]),
                             drafts=False, cache=c).page("root.welcome")

    assert 'href="/secret"' in allowed
    # The body, past the shell's own brand link.
    assert refused.split("</header>")[1].startswith("<p>see secret</p>")
    assert c.hits == 1


async def test_without_a_cache_it_renders_every_time_and_nothing_breaks():
    """None is a supported configuration, not a degraded one: `fswiki preview`
    passes it, because drafts have no key."""
    client = StubClient(document=PUBLISHED)
    pages = Pages(client, drafts=False, cache=None)
    first = await pages.page("root.welcome")
    assert first == await pages.page("root.welcome")


async def test_a_document_with_no_published_revision_is_not_cached():
    """A folder, or a document nobody has published. `version` is null, so
    there is no key -- and inventing one would collide across revisions."""
    c = Cache()
    client = StubClient(document={"id": "doc-9", "path": "root.empty",
                                  "version": None, "content": None})
    pages = Pages(client, drafts=False, cache=c)
    await pages.page("root.empty")
    assert len(c) == 0


def test_the_default_bound_is_a_size_and_not_a_count():
    assert render_cache.DEFAULT_MAX_BYTES == 32 * 1024 * 1024
