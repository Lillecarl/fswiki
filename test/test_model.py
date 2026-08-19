"""Folding the caller's drafts over the published manifest into one tree.

The whole point of a working copy is that your uncommitted work appears *in
place*: you edit `guide.md`, and `guide.md` reads back what you wrote. That is
this module's job, and everything above it — readdir, lookup, getattr, the
xattrs — sees only the result.

The mount tests cover this end to end, which is the right way to know the
wiring is real and the wrong way to reach the awkward shapes: a draft for a
document that has become invisible, a create three folders deep into a tree
that does not have them, a caller who can see nothing at all. Those are one
dict here and twenty seconds through a mount.

Pure functions: no stack, no network, no mount.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fswiki_core import naming
from fswiki_fuse import model


def doc(path, *, id=None, folder=False, version=1, caps=("read",),
        content_type=naming.DEFAULT_CONTENT_TYPE, **extra):
    """One manifest row, with the fields the server actually sends."""
    labels = naming.ltree_labels(path)
    return {
        "id": id or f"id:{path}",
        "path": path,
        "slug": labels[-1],
        "is_folder": folder,
        "content_type": None if folder else content_type,
        "version": version,
        "capabilities": list(caps),
        **extra,
    }


def draft(operation, path, **extra):
    return {"operation": operation, "path": path, **extra}


ROOT = doc("root", folder=True, caps=("read", "write"))
FOLDER = doc("root.eng", folder=True, caps=("read", "write"))
GUIDE = doc("root.eng.guide", version=7, caps=("read", "write"), size=11)


def names(tree, key):
    return sorted(tree.children[key])


# --- the published tree, with nothing over it ------------------------------

def test_the_manifest_alone_builds_the_tree():
    tree = model.build([ROOT, FOLDER, GUIDE], [])
    assert tree.root_key == ROOT["id"]
    assert names(tree, ROOT["id"]) == ["eng"]
    assert names(tree, FOLDER["id"]) == ["guide.md"]


def test_children_are_indexed_by_the_name_ls_prints():
    """Not by slug: lookup arrives from the kernel with a filename, extension
    and all, and a table keyed on slugs would miss every one of them."""
    tree = model.build([ROOT, FOLDER, GUIDE], [])
    assert tree.child(FOLDER["id"], "guide.md") is not None
    assert tree.child(FOLDER["id"], "guide") is None


def test_a_folder_has_no_extension():
    tree = model.build([ROOT, FOLDER, GUIDE], [])
    assert tree.get(FOLDER["id"]).name == "eng"


def test_the_content_type_picks_the_extension():
    tree = model.build([ROOT, doc("root.data", content_type="application/json")], [])
    assert tree.get("id:root.data").name == "data.json"


def test_a_missing_content_type_is_markdown():
    """The server can leave it null; a file with no extension in a directory
    listing would be a scratch file to every other layer."""
    row = doc("root.guide")
    row["content_type"] = None
    tree = model.build([ROOT, row], [])
    assert tree.get(row["id"]).name == "guide.md"


def test_writable_follows_the_acl():
    tree = model.build([ROOT, doc("root.ro", caps=("read",)),
                        doc("root.rw", caps=("read", "write"))], [])
    assert not tree.get("id:root.ro").writable
    assert tree.get("id:root.rw").writable


def test_published_means_it_has_a_revision():
    tree = model.build([ROOT, doc("root.guide", version=3)], [])
    assert tree.get("id:root.guide").published
    assert not tree.get("id:root.guide").has_draft


def test_an_unknown_key_is_absent_rather_than_an_error():
    """lookup() and getattr() arrive with whatever the kernel still believes,
    which after a refresh can be a document that has gone. Returning None lets
    the caller raise ENOENT; raising here would take the mount down."""
    tree = model.build([ROOT], [])
    assert tree.get("nonsense") is None
    assert tree.child(ROOT["id"], "nothing.md") is None
    assert tree.child("nonsense", "nothing.md") is None


def test_a_timestamp_is_read_from_the_version_then_the_document():
    stamp = "2024-03-04T05:06:07+00:00"
    tree = model.build([ROOT, doc("root.a", version_created_at=stamp),
                        doc("root.b", updated_at=stamp)], [])
    assert tree.get("id:root.a").mtime == datetime.fromisoformat(stamp)
    assert tree.get("id:root.b").mtime == datetime.fromisoformat(stamp)


def test_a_row_with_no_timestamp_at_all_still_has_one():
    """stat() must answer. `now` is a lie, but it is a lie no tool trips over,
    and the alternative is a FUSE handler raising."""
    before = datetime.now(timezone.utc)
    tree = model.build([ROOT, doc("root.a")], [])
    assert tree.get("id:root.a").mtime >= before


# --- drafts laid over it ---------------------------------------------------

def test_an_edit_appears_in_place():
    """The single most important property in the module: the file you edited
    reads back as what you wrote, at the path it already had."""
    tree = model.build([ROOT, FOLDER, GUIDE],
                       [draft("update", "root.eng.guide",
                              document_id=GUIDE["id"], content="much longer text\n")])
    node = tree.get(GUIDE["id"])
    assert node.has_draft
    assert node.size == len("much longer text\n")
    assert node.published          # still has a published revision behind it
    assert names(tree, FOLDER["id"]) == ["guide.md"]


def test_size_is_counted_in_bytes():
    """stat() reports bytes and read() returns bytes. A size in characters
    makes every tool that reads to EOF by size truncate multibyte text."""
    tree = model.build([ROOT, GUIDE],
                       [draft("update", "root.eng.guide",
                              document_id=GUIDE["id"], content="日本語\n")])
    assert tree.get(GUIDE["id"]).size == len("日本語\n".encode("utf-8"))


def test_a_create_appears_as_a_file_that_is_not_published():
    tree = model.build([ROOT, FOLDER],
                       [draft("create", "root.eng.new", content="hello")])
    node = tree.child(FOLDER["id"], "new.md")
    assert node is not None
    assert node.key == "draft:root.eng.new"
    assert node.document_id is None
    assert not node.published
    assert node.has_draft
    assert node.size == 5


def test_a_create_is_writable_because_it_is_yours():
    """There is no published document yet, so there is no ACL to consult. The
    author can obviously edit their own draft; push is where the server gets
    its say."""
    tree = model.build([ROOT], [draft("create", "root.new")])
    assert tree.get("draft:root.new").writable


def test_a_create_carries_its_own_content_type():
    tree = model.build([ROOT], [draft("create", "root.data",
                                      content_type="application/json")])
    assert tree.get("draft:root.data").name == "data.json"


def test_a_retirement_leaves_the_working_copy():
    """Gone from the tree, still on the server until pushed — which is what
    makes `fswiki revert` able to put it back."""
    tree = model.build([ROOT, FOLDER, GUIDE],
                       [draft("delete", "root.eng.guide", document_id=GUIDE["id"])])
    assert tree.get(GUIDE["id"]) is None
    assert names(tree, FOLDER["id"]) == []


def test_a_retirement_can_be_found_by_path_alone():
    """A draft row need not carry a document id, and the path is the one
    description both sources share."""
    tree = model.build([ROOT, FOLDER, GUIDE], [draft("delete", "root.eng.guide")])
    assert tree.get(GUIDE["id"]) is None


def test_a_move_shows_the_file_where_it_is_going():
    """Not where it still is on the server. Anything else means `mv` appears
    to have done nothing until you push."""
    tree = model.build([ROOT, FOLDER, GUIDE],
                       [draft("move", "root.moved", document_id=GUIDE["id"])])
    node = tree.get(GUIDE["id"])
    assert node.path == "root.moved"
    assert node.slug == "moved"
    assert node.name == "moved.md"
    assert names(tree, ROOT["id"]) == ["eng", "moved.md"]
    assert names(tree, FOLDER["id"]) == []


def test_a_move_that_goes_nowhere_is_harmless():
    tree = model.build([ROOT, FOLDER, GUIDE],
                       [draft("move", "root.eng.guide", document_id=GUIDE["id"])])
    assert tree.get(GUIDE["id"]).path == "root.eng.guide"
    assert names(tree, FOLDER["id"]) == ["guide.md"]


def test_a_draft_for_something_no_longer_visible_is_omitted():
    """The document was retired by someone else, or the ACL changed under you.
    push will report it — the working copy just does not show a file it has no
    place to put."""
    tree = model.build([ROOT], [draft("update", "root.gone",
                                      document_id="id:root.gone", content="x")])
    assert tree.get("id:root.gone") is None
    assert names(tree, ROOT["id"]) == []


def test_an_update_with_no_content_leaves_the_published_size():
    """A draft row exists for reasons other than content — a move carries
    none — and zeroing the size would make the file look empty."""
    tree = model.build([ROOT, GUIDE],
                       [draft("update", "root.eng.guide", document_id=GUIDE["id"])])
    assert tree.get(GUIDE["id"]).size == 11


# --- folders nobody published ---------------------------------------------

def test_a_create_invents_the_folders_it_needs():
    """push auto-creates them server-side, so the working copy must show them
    or `mkdir -p && vim` fails at the first component."""
    tree = model.build([ROOT], [draft("create", "root.a.b.c", content="x")])
    a = tree.child(ROOT["id"], "a")
    assert a is not None and a.is_folder and a.synthetic
    b = tree.child(a.key, "b")
    assert b is not None and b.synthetic
    assert tree.child(b.key, "c.md") is not None


def test_an_invented_folder_is_writable():
    """It stands for something push will create, and refusing writes beneath
    it would make the tree it exists to enable unusable."""
    tree = model.build([ROOT], [draft("create", "root.a.b")])
    assert tree.get("synthetic:root.a").writable
    assert not tree.get("synthetic:root.a").published


def test_a_caller_who_may_see_nothing_still_gets_a_mount():
    """erin's manifest is empty — not even a root row. An empty directory is a
    correct answer; a mount that fails to assemble is not."""
    tree = model.build([], [])
    assert tree.root_key == "synthetic:root"
    assert tree.get(tree.root_key).is_folder
    assert names(tree, tree.root_key) == []


def test_a_real_root_is_preferred_to_an_invented_one():
    tree = model.build([ROOT, GUIDE], [])
    assert tree.root_key == ROOT["id"]
    assert not tree.get(tree.root_key).synthetic


def test_a_manifest_with_a_gap_in_it_is_assembled_anyway():
    """Inheritance can be blocked partway down, so a caller sees `root` and
    `root.a.b` without `root.a`. Inventing the missing folder is the only way
    the deep document is reachable at all."""
    tree = model.build([ROOT, doc("root.a.b")], [])
    a = tree.child(ROOT["id"], "a")
    assert a is not None and a.synthetic
    assert tree.child(a.key, "b.md") is not None


def test_a_tree_built_with_a_hole_does_not_raise():
    """Unreachable through build(), which synthesises ancestors first. Tested
    directly because the consequence of being wrong is not a failed syscall:
    an exception here comes out of pyfuse3.main() and unmounts everything."""
    orphan = model.Node(key="k", path="root.a.b", slug="b", is_folder=False)
    tree = model.Tree({"k": orphan}, "k")
    assert tree.get("k") is orphan
    assert tree.children["k"] == {}
