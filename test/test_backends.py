"""The client under both async backends.

`fswiki_core.client` is built on httpx, which works under trio as well as
asyncio because httpcore is built on anyio. That is not a decorative fact: the
FUSE mount runs on trio — pyfuse3's native backend, chosen so cancellation
behaves the way the FUSE protocol wants — while the CLI and the preview server
run on asyncio. The same client code has to work in both.

A claim like that is exactly what the anyio pytest plugin is for, and the
reason this suite uses it rather than pytest-asyncio: overriding one fixture
runs every test in the module against each backend in turn.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request):
    """Overrides the session default, for this module only.

    Doing this suite-wide would double a run that spends its time waiting on a
    filesystem, to re-prove one module's worth of property.
    """
    return request.param


async def test_a_read_works_on_either_backend(stack, client, anyio_backend):
    c = await client("bob")
    assert await c.content(stack.doc("root.public.welcome"))


async def test_a_write_works_on_either_backend(stack, client, clean, anyio_backend):
    c = await client("bob")
    draft = await c.put_draft(
        author_id=stack.who("bob"),
        operation="update",
        document_id=stack.doc("root.engineering.onboarding"),
        path="root.engineering.onboarding",
        content=f"written under {anyio_backend}\n",
        base_version=stack.tip("root.engineering.onboarding"),
    )
    assert draft["path"] == "root.engineering.onboarding"
    assert stack.count("select count(*) from wiki.draft") == 1


async def test_concurrency_works_on_either_backend(stack, client, anyio_backend):
    """Several requests in flight on one pool, which is where a backend
    mismatch would actually surface rather than on a single round trip."""
    import anyio

    c = await client("bob")
    paths = ["root.public.welcome", "root.public.guide.index",
             "root.public.guide.mounting", "root.engineering.onboarding"]
    seen = {}

    async def fetch(path):
        seen[path] = await c.document(path)

    async with anyio.create_task_group() as tg:
        for path in paths:
            tg.start_soon(fetch, path)

    assert set(seen) == set(paths)
    assert all(row is not None for row in seen.values())
