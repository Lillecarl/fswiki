"""PostgREST over HTTP.

Async on top of httpx, which works under trio as well as asyncio because
httpcore is built on anyio. Nothing here knows about FUSE.

One rule runs through the whole module: **reads come from
`wiki.syncable_document`, never `wiki.current_document`**. The two differ
exactly where a deny-sync ACE sits, and `current_document` would hand back
content the server has said must not be copied to a laptop.

That rule is about *mirroring*, which is what every client here does, and it
is the default. It is the wrong rule for something that renders a page and
keeps nothing: denying `sync` is meant to leave a document readable in a
browser precisely so that every view costs a request the server can log, and a
reader that went through the sync view could not serve those pages at all.
`tree="read"` is that reader, and nothing that writes to a local disk may ask
for it.
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


def _unhex(value: str | bytes | None) -> bytes | None:
    """PostgREST hands `bytea` back the way Postgres prints it: `\\x0102`."""
    if value is None or isinstance(value, bytes):
        return value
    return bytes.fromhex(value[2:] if value.startswith("\\x") else value)


class Client:
    """One identity, and by default a connection pool of its own.

    Two identities, strictly speaking, when impersonating: the token stays the
    caller's and the server resolves the borrowed one. Nothing here decides
    anything about permissions — the headers are a request, and the server
    refuses them unless a grant says otherwise.

    A pool to itself is right for the CLI and the mount: one human, one
    process, connections worth keeping warm for as long as it runs. It is
    wrong for anything serving whoever is asking, where the identity changes
    per request — see `ClientPool`, and pass its transport in here.
    """

    # Which tree this client reads. "sync" is what a mirror sees and is the
    # default everywhere; "read" is what a browser sees. They differ exactly
    # where a deny-sync ACE sits, and the server enforces the difference: the
    # two RPCs below are two separate grants, so asking for the read tree is a
    # request the server may refuse rather than a decision made here.
    _TREES = {
        "sync": ("syncable_document", "list_documents", "read_document"),
        "read": ("current_document", None, "view_document"),
    }

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 15.0,
                 act_as: str | None = None,
                 act_as_groups: list[str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 tree: str = "sync") -> None:
        if tree not in self._TREES:
            raise ValueError(f"tree must be 'sync' or 'read', not {tree!r}")
        self.tree = tree
        self._view, self._list_rpc, self._read_rpc = self._TREES[tree]
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if act_as and act_as_groups:
            raise ValueError("act as a person or a membership, not both")
        if tree == "read" and (act_as or act_as_groups):
            # Impersonation refuses a read-only transaction so it can always
            # write its own log, so an impersonated read goes over a volatile
            # RPC rather than a GET. wiki.list_documents() is that RPC and it
            # returns setof syncable_document; there is no current_document
            # equivalent because nothing has needed one. Refused here rather
            # than at the first manifest(), so the failure names the reason.
            raise ValueError("the read tree has no impersonated transport yet: "
                             "wiki.list_documents() returns the sync tree")
        if act_as:
            headers["Fswiki-Act-As"] = act_as
        if act_as_groups:
            headers["Fswiki-Act-As-Groups"] = ",".join(act_as_groups)
        # Every read below has two forms because of this flag. See _reading().
        self.impersonating = bool(act_as or act_as_groups)
        # Kept in plain sight: every caller that reports "cannot reach it" wants
        # to name the address, and digging it back out of the httpx client is
        # both awkward and a way to print something subtly different from what
        # was asked for.
        self.base_url = base_url.rstrip("/")
        # httpx keeps the connection pool in the transport, so a borrowed
        # transport is a borrowed pool. Whether this instance owns one has to
        # be remembered: AsyncClient.aclose() closes its transport
        # unconditionally, and there is no flag on it that says otherwise.
        self._owns_transport = transport is None
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Done with this identity — and with the connections, if they are ours.

        Closing a borrowed pool would take every other identity's live
        connections down with it, which is a bug that appears only under
        concurrency and looks like the server hanging up at random.
        """
        if self._owns_transport:
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
            f"/{self._view}", self._list_rpc,
            params={"select": MANIFEST_COLUMNS, "order": "path"},
        )
        return self._rows(r)

    async def outline(self) -> list[dict]:
        """Path and kind for every visible document. The tree, and nothing else.

        Separate from manifest() because of what manifest() costs. Its
        `capabilities` column is wiki.capabilities_at() per row -- one ACL walk
        per capability per document -- and everything that renders a page
        throws all of it away: a link is resolvable or it is not, and that is
        answered by the path being in this list.

        Measured against the dev fixtures, 17 documents visible:

            manifest, full columns   224.15 ms   6801 B
            this, through the read tree 37.97 ms   647 B

        The mount is what needs the full manifest -- it stats every entry and
        publishes capabilities as an xattr -- and it is welcome to it.
        """
        r = await self._reading(
            f"/{self._view}", self._list_rpc,
            params={"select": "path,is_folder", "order": "path"},
        )
        return self._rows(r)

    async def content(self, document_id: str, *, event: dict | None = None) -> bytes:
        """The published body of one document.

        Through this client's tree: `syncable_document` by default, so a
        document that is readable but not syncable comes back as no rows
        rather than as content, and `current_document` under `tree="read"`,
        where it comes back as the page it was always meant to be.

        With `event`, the same read goes over the matching audited RPC, which
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
                f"/rpc/{self._read_rpc}",
                json={"p_document": document_id, "p_event": event},
            )
        else:
            r = await self._http.get(
                f"/{self._view}",
                params={"select": "content", "id": f"eq.{document_id}"},
            )
        rows = self._rows(r)
        if not rows:
            raise LookupError(
                f"document {document_id} is not in the {self.tree} tree, or is gone")
        body = rows[0].get("content")
        return b"" if body is None else body.encode("utf-8")

    async def document(self, path: str) -> dict | None:
        """One document by path, with its body, or None if it is not there.

        Through this client's tree like every other read here, so "not there"
        and "not yours" are the same answer — which is the answer a
        renderer wants, since telling them apart is how a link graph leaks.
        """
        r = await self._reading(
            f"/{self._view}", "document_at",
            params={"select": "id,path,content,content_type,version,is_attachment",
                    **({} if self.impersonating else {"path": f"eq.{path}"})},
            body={"p_path": path},
        )
        rows = self._rows(r)
        return rows[0] if rows else None

    async def search(self, query: str, *, limit: int = 20,
                     drafts: bool = False) -> list[dict]:
        """Ranked matches for `query`, filtered by whatever this caller may read.

        Always the RPC form, with no GET beside it. `wiki.search` is declared
        volatile so that one endpoint serves an impersonated caller too -- see
        the head of runtime/078_search.sql -- which is also why this needs
        none of `_reading`'s two-transport dance.

        `drafts=True` asks the other function, which returns the caller's own
        unpublished work and nobody else's. It is for the preview; the server
        never asks.
        """
        rpc = "search_drafts" if drafts else "search"
        r = await self._http.post(f"/rpc/{rpc}",
                                  json={"p_query": query, "p_limit": limit})
        return self._rows(r)

    # -- attachments -------------------------------------------------------

    async def attachment(self, path: str) -> dict | None:
        """One attachment by path, bytes included, or None.

        Always the RPC form: `wiki.attachment_at` is volatile so that one
        endpoint serves an impersonated caller too, the same reasoning as
        `search`. SECURITY INVOKER, so a file you may not read is a file that
        is not there -- which is the answer every other read here gives.

        **The bytes arrive hex-encoded**, because PostgREST renders `bytea`
        the way Postgres does and JSON has no bytes. That doubles them in
        transit and costs a decode: measured at about 1.5 ms for a 100 kB
        image and 150 ms at the 10 MiB cap. Accepted rather than split into a
        second request for a scalar column with
        `Accept: application/octet-stream`, which would be exact and would
        also make every image two round trips instead of one. If a wiki ever
        serves large files often, that is the fix, and it is a change to this
        method alone.
        """
        r = await self._http.post("/rpc/attachment_at", json={"p_path": path})
        rows = self._rows(r)
        if not rows:
            return None
        row = dict(rows[0])
        row["bytes"] = _unhex(row.get("bytes"))
        row["sha256"] = _unhex(row.get("sha256"))
        return row

    async def attach(self, path: str, media_type: str, payload: bytes) -> dict:
        """Store or replace an attachment. Returns `{document_id, created, byte_size}`.

        The size limit is the database's, so a file over it comes back as a
        PostgrestError naming the cap rather than as a refusal this client
        invented. A client-side check would be a second limit to keep in step,
        and the one that matters is the one psql also has to obey.
        """
        r = await self._http.post(
            "/rpc/attach",
            json={"p_path": path, "p_media_type": media_type,
                  "p_bytes": "\\x" + payload.hex()})
        rows = self._rows(r)
        return rows[0] if rows else {}

    async def detach(self, path: str) -> bool:
        """Remove an attachment permanently. False if there was none.

        Permanent, and there is no retire to offer instead: an attachment has
        no revisions to fall back on. It needs `purge` for that reason.
        """
        r = await self._http.post("/rpc/detach", json={"p_path": path})
        if r.status_code >= 400:
            raise PostgrestError(r)
        return r.json() is True

    async def max_attachment_bytes(self) -> int | None:
        """The wiki's upload cap, for saying so before a big file is sent."""
        r = await self._http.post("/rpc/max_attachment_bytes", json={})
        if r.status_code >= 400:
            return None
        value = r.json()
        return int(value) if value is not None else None

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


class ClientPool:
    """Many identities, one set of connections.

    A server renders pages for whoever is asking, so the token changes from
    one request to the next and a `Client` per request would mean a fresh TCP
    connection to PostgREST per page view — handshake and all, for a request
    that is usually a single indexed SELECT.

    What has to stay per-identity is the headers: the bearer token and the
    impersonation headers are what make a request that caller's, and they are
    the only reason this cannot simply be one shared `Client`. Everything
    below them — sockets, keep-alive, DNS — is identity-agnostic, so it lives
    here and the clients borrow it.

    Nothing about permissions changes. Two callers sharing a socket still
    arrive at PostgREST with their own tokens, and RLS sees exactly what it
    saw before; connection reuse is beneath the layer that decides anything.
    """

    def __init__(self, *, limits: httpx.Limits | None = None) -> None:
        self._transport = httpx.AsyncHTTPTransport(
            limits=limits if limits is not None else httpx.Limits())

    def client(self, base_url: str, token: str | None, **kwargs: Any) -> Client:
        """A client for one identity, on the shared connections.

        Cheap enough to build per request: it is a header dict and a wrapper.
        Its `aclose()` is a no-op on the pool, so callers may close it as
        usual without reaching anyone else's connections.
        """
        return Client(base_url, token, transport=self._transport, **kwargs)

    async def aclose(self) -> None:
        """Close the connections. The pool owns them; nobody else may."""
        await self._transport.aclose()
