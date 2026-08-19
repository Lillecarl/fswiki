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
    "id,path,slug,is_folder,title,owner_id,version,version_author_id,size,"
    "content_type,updated_at,version_created_at,capabilities"
)

DRAFT_COLUMNS = (
    "id,operation,document_id,path,content,content_type,base_version,updated_at,"
    "state,merged_from,pre_merge_content"
)


# What "cannot reach it" looks like: connection refused, DNS failure, TLS
# failure, a timeout. Re-exported rather than wrapped, because wrapping would
# mean a try/except around every call in this module to gain nothing — but
# callers should not have to import httpx to catch the one case every one of
# them has to handle.
#
# Deliberately not an OSError: httpx does not derive from it, so catching
# OSError alone lets a refused connection out as a traceback. That is what it
# did, until a test asked what `fswiki whoami` says when the server is down.
Unreachable = httpx.TransportError


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
    """One connection pool, one identity.

    Two identities, strictly speaking, when impersonating: the token stays the
    caller's and the server resolves the borrowed one. Nothing here decides
    anything about permissions — the headers are a request, and the server
    refuses them unless a grant says otherwise.
    """

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 15.0,
                 act_as: str | None = None,
                 act_as_groups: list[str] | None = None) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if act_as and act_as_groups:
            raise ValueError("act as a person or a membership, not both")
        if act_as:
            headers["Fswiki-Act-As"] = act_as
        if act_as_groups:
            headers["Fswiki-Act-As-Groups"] = ",".join(act_as_groups)
        # Every read below has two forms because of this flag. See _reading().
        self.impersonating = bool(act_as or act_as_groups)
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

    async def _reading(self, path: str, rpc: str, *, params: dict | None = None,
                       body: dict | None = None) -> httpx.Response:
        """One read, over whichever transport the server will allow.

        A GET runs in a read-only transaction, and impersonation refuses any
        transaction it cannot write its own log into — so while impersonating,
        every read goes through the volatile RPC form instead. The rows are the
        same: the functions are SECURITY INVOKER over the same views, and the
        `select` parameter works on either.

        Not a fallback and not a retry. Which transport is correct is known
        before the request, so there is no failed round trip to pay for.
        """
        if self.impersonating:
            return await self._http.post(f"/rpc/{rpc}", params=params, json=body or {})
        return await self._http.get(path, params=params)

    # -- identity ----------------------------------------------------------

    async def whoami(self) -> str | None:
        """The caller's principal id, or None if the token resolves to nobody.

        Worth calling at mount time even though nothing needs the value yet: it
        turns an expired token into one clear error instead of an empty wiki.
        """
        # current_user_id() is `stable` and so read-only, which impersonation
        # refuses; acting_as() is the volatile form of the same question. A
        # client that could borrow an identity but not name it would be a poor
        # tool for the one job impersonation has.
        rpc = "acting_as" if self.impersonating else "current_user_id"
        r = await self._http.post(f"/rpc/{rpc}", json={})
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
        # change_token() is `stable`, so PostgREST runs it read-only even over
        # POST, which impersonation refuses. `changed()` is the volatile form.
        # Not a nicety: without it an impersonated mount refetches the whole
        # manifest on every poll, which is six kilobytes for nothing and a
        # steady drip of requests into the impersonation log.
        rpc = "changed" if self.impersonating else "change_token"
        r = await self._http.post(f"/rpc/{rpc}", json={})
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
        r = await self._reading(
            "/syncable_document", "list_documents",
            params={"select": MANIFEST_COLUMNS, "order": "path"},
        )
        return self._rows(r)

    async def content(self, document_id: str, *, event: dict | None = None) -> bytes:
        """The published body of one document.

        Through `syncable_document`, so a document that is readable but not
        syncable comes back as no rows rather than as content.

        With `event`, the same read goes over `POST /rpc/read_document`, which
        records the access in the transaction that serves the bytes. The verb
        is the point: PostgREST runs GET in a read-only transaction, so a GET
        cannot write its own audit row, and POST can. Visibility is identical —
        the function is SECURITY INVOKER over the same view — so this is the
        same read, witnessed.

        Without `event` it stays a GET, which is cacheable and idempotent and
        the right thing when nobody is auditing.
        """
        if event is not None or self.impersonating:
            # p_event is nullable, so the RPC is also the un-audited read. That
            # matters while impersonating, where the GET below is not available
            # at all -- and it costs nothing, since an impersonated read writes
            # no access event anyway (the hook wrote an impersonation_event
            # instead, which is the truer record of what happened).
            r = await self._http.post(
                "/rpc/read_document",
                json={"p_document": document_id, "p_event": event},
            )
        else:
            r = await self._http.get(
                "/syncable_document",
                params={"select": "content", "id": f"eq.{document_id}"},
            )
        rows = self._rows(r)
        if not rows:
            raise LookupError(f"document {document_id} is not syncable, or is gone")
        body = rows[0].get("content")
        return b"" if body is None else body.encode("utf-8")

    async def document(self, path: str) -> dict | None:
        """One document by path, with its body, or None if it is not there.

        Through `syncable_document` like every other read here, so "not there"
        and "not yours to mirror" are the same answer — which is the answer a
        renderer wants, since telling them apart is how a link graph leaks.
        """
        r = await self._reading(
            "/syncable_document", "document_at",
            params={"select": "id,path,content,content_type,version",
                    **({} if self.impersonating else {"path": f"eq.{path}"})},
            body={"p_path": path},
        )
        rows = self._rows(r)
        return rows[0] if rows else None

    # -- drafts ------------------------------------------------------------

    async def drafts(self) -> list[dict]:
        r = await self._reading("/draft", "list_drafts", params={"select": DRAFT_COLUMNS})
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

    async def revision(self, document_id: str, version: int) -> str | None:
        """The content of one historical revision, or None if it is not there.

        This is the merge base. It comes from `document_version` directly rather
        than a view, because the tip views deliberately show only the open
        revision — the ancestor an edit descends from is by definition closed.

        Note the policy on that table gates on `read`, not `sync`, so a caller
        who may read a document but not mirror it can still pull an old revision
        this way. That asymmetry predates this method; it is worth closing, and
        `syncable_document` is the shape to copy when doing so.
        """
        r = await self._http.get(
            "/document_version",
            params={
                "select": "content",
                "document_id": f"eq.{document_id}",
                "version": f"eq.{version}",
            },
        )
        rows = self._rows(r)
        return rows[0]["content"] if rows else None

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

    async def record_opens(self, events: list[dict]) -> int:
        """File a batch of access events. Returns how many were new.

        Idempotent on each event's id, so a client that never saw the response
        can resend the batch and get 0 back rather than duplicate rows.
        """
        r = await self._http.post("/rpc/record_opens", json={"p_events": events})
        if r.status_code >= 400:
            raise PostgrestError(r)
        value = r.json()
        return value if isinstance(value, int) else 0

    async def begin_merge(self, path: str, content: str, merged_from: int,
                          *, conflicted: bool) -> dict | None:
        """Record that a merge rewrote a draft, keeping the text it replaced."""
        return await self._merge_rpc("begin_merge", {
            "p_path": path,
            "p_content": content,
            "p_merged_from": merged_from,
            "p_conflicted": conflicted,
        })

    async def resolve_merge(self, path: str) -> dict | None:
        """Finish a merge: rebase onto what it pulled in and drop the backup."""
        return await self._merge_rpc("resolve_merge", {"p_path": path})

    async def abort_merge(self, path: str) -> dict | None:
        """Back out: restore the text as it was before the merge."""
        return await self._merge_rpc("abort_merge", {"p_path": path})

    async def _merge_rpc(self, name: str, payload: dict) -> dict | None:
        r = await self._http.post(
            f"/rpc/{name}",
            json=payload,
            # The functions return the row they touched, and RLS filters rather
            # than raising, so no row back means the draft was not the caller's
            # to change. That has to read as a failure, not a silent success.
            headers={"Accept": "application/vnd.pgrst.object+json"},
        )
        if r.status_code == 406:
            return None
        if r.status_code >= 400:
            raise PostgrestError(r)
        return r.json()

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
