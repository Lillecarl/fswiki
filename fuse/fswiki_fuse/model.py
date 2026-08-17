"""The tree the mount presents: the published manifest with drafts laid over it.

A draft is the author's uncommitted work, and the whole point of the working
copy is that it appears *in place* — you edit `guide.md`, and `guide.md` reads
back what you wrote, not what the server still holds. So the tree is assembled
once from two sources and everything above this layer sees a single answer.

Assembly is keyed on ltree paths rather than parent ids, because a draft can put
a document somewhere the server does not have it yet (a create, or a move), and
paths are the only description both sources share. Splitting a path is exact:
slugs cannot contain dots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from fswiki_core import naming

ROOT_PATH = "root"


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value)


@dataclass
class Node:
    """One entry in the mounted tree."""

    key: str  # stable identity, and what an inode is allocated against
    path: str  # ltree
    slug: str
    is_folder: bool
    content_type: str = naming.DEFAULT_CONTENT_TYPE

    document_id: str | None = None
    version: int | None = None
    size: int = 0
    mtime: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    capabilities: frozenset[str] = frozenset()
    owner_id: str | None = None
    title: str | None = None

    # The draft row covering this path, if the author has one.
    draft: dict | None = None
    # A folder we invented because a draft put something underneath it. It has
    # no server-side existence and no ACL of its own.
    synthetic: bool = False

    @property
    def name(self) -> str:
        return naming.filename(self.slug, self.content_type, self.is_folder)

    @property
    def writable(self) -> bool:
        # A synthetic folder is a placeholder for something push will create;
        # writing beneath it is allowed and will be judged server-side.
        return self.synthetic or "write" in self.capabilities

    @property
    def published(self) -> bool:
        return self.version is not None

    @property
    def has_draft(self) -> bool:
        return self.draft is not None


class Tree:
    """An immutable snapshot. Refreshing builds a new one."""

    def __init__(self, nodes: dict[str, Node], root_key: str) -> None:
        self.nodes = nodes
        self.root_key = root_key
        self.by_path = {n.path: n.key for n in nodes.values()}

        self.children: dict[str, dict[str, str]] = {k: {} for k in nodes}
        for node in nodes.values():
            parent_path = naming.ltree_parent(node.path)
            if parent_path is None:
                continue
            parent_key = self.by_path.get(parent_path)
            if parent_key is None:
                # Unreachable after _synthesise_ancestors, but a tree with a
                # hole in it must not take the mount down.
                continue
            self.children[parent_key][node.name] = node.key

    def get(self, key: str) -> Node | None:
        return self.nodes.get(key)

    def child(self, parent_key: str, name: str) -> Node | None:
        key = self.children.get(parent_key, {}).get(name)
        return self.nodes.get(key) if key else None


def build(manifest: list[dict], drafts: list[dict]) -> Tree:
    """Fold the published manifest and the caller's drafts into one tree."""
    nodes: dict[str, Node] = {}

    for row in manifest:
        node = Node(
            key=row["id"],
            document_id=row["id"],
            path=row["path"],
            slug=row["slug"],
            is_folder=row["is_folder"],
            content_type=row.get("content_type") or naming.DEFAULT_CONTENT_TYPE,
            version=row.get("version"),
            size=row.get("size") or 0,
            mtime=_parse_time(row.get("version_created_at") or row.get("updated_at")),
            capabilities=frozenset(row.get("capabilities") or ()),
            owner_id=row.get("owner_id"),
            title=row.get("title"),
        )
        nodes[node.key] = node

    _apply_drafts(nodes, drafts)
    root_key = _synthesise_ancestors(nodes)
    return Tree(nodes, root_key)


def _apply_drafts(nodes: dict[str, Node], drafts: list[dict]) -> None:
    by_path = {n.path: n for n in nodes.values()}
    by_document = {n.document_id: n for n in nodes.values() if n.document_id}

    for draft in drafts:
        op = draft["operation"]
        path = draft["path"]
        document_id = draft.get("document_id")

        if op == "delete":
            # Retired locally: gone from the working copy, still on the server
            # until pushed.
            target = by_document.get(document_id) if document_id else by_path.get(path)
            if target is not None:
                nodes.pop(target.key, None)
            continue

        if op == "create":
            slug = naming.ltree_labels(path)[-1]
            content = draft.get("content") or ""
            node = Node(
                key=f"draft:{path}",
                document_id=None,
                path=path,
                slug=slug,
                is_folder=False,
                content_type=draft.get("content_type") or naming.DEFAULT_CONTENT_TYPE,
                version=None,
                size=len(content.encode("utf-8")),
                mtime=_parse_time(draft.get("updated_at")),
                # Nothing is published yet, so there is no ACL to consult; the
                # author can obviously read and write their own draft, and push
                # is where the server gets its say.
                capabilities=frozenset({"read", "sync", "write"}),
                draft=draft,
            )
            nodes[node.key] = node
            continue

        target = by_document.get(document_id) if document_id else by_path.get(path)
        if target is None:
            # The draft outlived the document, or names something no longer
            # visible. Push will report it; the working copy just omits it.
            continue

        target.draft = draft
        if op == "move" and path != target.path:
            target.path = path
            target.slug = naming.ltree_labels(path)[-1]
        if draft.get("content") is not None:
            target.size = len(draft["content"].encode("utf-8"))
        target.mtime = _parse_time(draft.get("updated_at"))


def _synthesise_ancestors(nodes: dict[str, Node]) -> str:
    """Invent the folders a draft implied, and guarantee a root.

    Two cases need this. A draft creating `root.a.b` when `root.a` does not
    exist yet — push auto-creates the folders, so the working copy must show
    them. And a caller who may see nothing at all: erin gets an empty manifest,
    not even a root row, and an empty mount is still a mount.
    """
    by_path = {n.path: n for n in nodes.values()}

    for path in list(by_path):
        parent = naming.ltree_parent(path)
        while parent and parent not in by_path:
            node = Node(
                key=f"synthetic:{parent}",
                path=parent,
                slug=naming.ltree_labels(parent)[-1],
                is_folder=True,
                synthetic=True,
            )
            nodes[node.key] = node
            by_path[parent] = node
            parent = naming.ltree_parent(parent)

    root = by_path.get(ROOT_PATH)
    if root is None:
        root = Node(
            key=f"synthetic:{ROOT_PATH}",
            path=ROOT_PATH,
            slug=ROOT_PATH,
            is_folder=True,
            synthetic=True,
        )
        nodes[root.key] = root
    return root.key
