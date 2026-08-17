"""PostgREST over HTTP.

Async on top of httpx, which works under trio as well as asyncio because
httpcore is built on anyio. Nothing here knows about FUSE.

One rule runs through the whole module: **reads come from
`wiki.syncable_document`, never `wiki.current_document`**. The two differ
exactly where a deny-sync ACE sits, and `current_document` would hand back
content the server has said must not be copied to a laptop.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Everything the client needs to build a tree and stat every entry in it,
# without pulling a single document body.
MANIFEST_COLUMNS = (
    "id,path,slug,is_folder,title,owner_id,version,size,"
    "content_type,updated_at,version_created_at,capabilities"
)

DRAFT_COLUMNS = "id,operation,document_id,path,content,content_type,base_version,updated_at"


class PostgrestError(RuntimeError):
    """A non-2xx response, with the body PostgREST put in it."""

    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.body: Any
        try:
            self.body = response.json()
        except ValueError:
            self.body = response.text
        message = ""
        if isinstance(self.body, dict):
            message = self.body.get("message") or ""
        super().__init__(f"{response.request.method} {response.request.url}: "
                         f"{self.status} {message or self.body}")


class Client:
    """One connection pool, one identity."""

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 15.0) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _rows(response: httpx.Response) -> list[dict]:
        if response.status_code >= 400:
            raise PostgrestError(response)
        if not response.content:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else [payload]

    # -- identity ----------------------------------------------------------

    async def whoami(self) -> str | None:
        """The caller's principal id, or None if the token resolves to nobody.

        Worth calling at mount time even though nothing needs the value yet: it
        turns an expired token into one clear error instead of an empty wiki.
        """
        r = await self._http.post("/rpc/current_user_id", json={})
        if r.status_code >= 400:
            raise PostgrestError(r)
        value = r.json()
        return value if isinstance(value, str) else None

    async def change_token(self) -> str | None:
        """A value that moves whenever the database is written.

        Eleven bytes against six kilobytes for the manifest, which is what makes
        a short poll interval affordable. None if the server predates the
        function — callers should fall back to refetching unconditionally.
        """
        r = await self._http.post("/rpc/change_token", json={})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise PostgrestError(r)
        value = r.json()
        return value if isinstance(value, str) else None

    # -- reads -------------------------------------------------------------

    async def manifest(self) -> list[dict]:
        """Every document this caller may mirror, in one request.

        Deliberately one round trip rather than a walk. The whole tree is a few
        KB — bob's is 3.6 KB against the dev fixtures — so paging it in per
        directory would cost more requests to save nothing.
        """
        r = await self._http.get(
            "/syncable_document",
            params={"select": MANIFEST_COLUMNS, "order": "path"},
        )
        return self._rows(r)

    async def content(self, document_id: str) -> bytes:
        """The published body of one document.

        Through `syncable_document`, so a document that is readable but not
        syncable comes back as no rows rather than as content.
        """
        r = await self._http.get(
            "/syncable_document",
            params={"select": "content", "id": f"eq.{document_id}"},
        )
        rows = self._rows(r)
        if not rows:
            raise LookupError(f"document {document_id} is not syncable, or is gone")
        body = rows[0].get("content")
        return b"" if body is None else body.encode("utf-8")

    # -- drafts ------------------------------------------------------------

    async def drafts(self) -> list[dict]:
        r = await self._http.get("/draft", params={"select": DRAFT_COLUMNS})
        return self._rows(r)

    async def put_draft(
        self,
        *,
        author_id: str,
        operation: str,
        path: str,
        document_id: str | None = None,
        content: str | None = None,
        content_type: str | None = None,
        base_version: int | None = None,
        message: str | None = None,
    ) -> dict:
        """Create or replace the caller's draft at `path`.

        Upserts on the (author_id, path) unique constraint rather than the
        primary key, because the client thinks in paths and never learns the
        draft's id.
        """
        payload = {
            "author_id": author_id,
            "operation": operation,
            "path": path,
            "document_id": document_id,
            "content": content,
            "base_version": base_version,
            "message": message,
        }
        if content_type is not None:
            payload["content_type"] = content_type

        r = await self._http.post(
            "/draft",
            params={"on_conflict": "author_id,path"},
            json=payload,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        rows = self._rows(r)
        return rows[0] if rows else {}

    async def push(self, message: str | None, paths: list[str] | None = None) -> list[dict]:
        """Promote drafts to published revisions.

        All or nothing: if any returned row has a status other than 'published',
        **nothing was written** and the drafts are still there. The caller must
        look at every row — the first one being fine says nothing about the rest.

        `paths` selects a subset; None means every draft the caller has.
        """
        payload: dict[str, object] = {"p_message": message}
        if paths is not None:
            payload["p_paths"] = paths
        r = await self._http.post("/rpc/push", json=payload)
        return self._rows(r)

    async def delete_draft(self, path: str) -> bool:
        """Drop the caller's draft at `path`. True if there was one.

        RLS filters rather than raising, so a draft belonging to someone else is
        simply not among the rows deleted and this returns False. Never read
        "no error" as "it worked".
        """
        r = await self._http.delete(
            "/draft",
            params={"path": f"eq.{path}"},
            headers={"Prefer": "return=representation"},
        )
        return bool(self._rows(r))
