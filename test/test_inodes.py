"""The inode table: uuids in, 64-bit integers out, and the kernel's bookkeeping.

Two properties, both easy to get wrong and both invisible until something has
a file open. **Stability**: the same key keeps the same inode for as long as
the kernel remembers it, across every manifest refresh. **Lifetime**: every
entry handed out is counted, and only released when the kernel says so.

Getting stability wrong swaps a file underneath an editor. Getting lifetime
wrong either dangles an open handle or leaks an entry per file ever listed,
and a mount is a long-running process.

Pure data structure: no stack, no network, no mount.
"""

from __future__ import annotations

import pytest

from fswiki_fuse.inodes import ROOT_INODE, InodeTable


@pytest.fixture
def table():
    return InodeTable()


def test_the_root_is_inode_one(table):
    """Not a convention we chose — the kernel assumes it."""
    table.pin_root("root-uuid")
    assert table.inode_for("root-uuid") == ROOT_INODE
    assert table.key_for(ROOT_INODE) == "root-uuid"


def test_pinning_the_same_root_twice_changes_nothing(table):
    table.pin_root("root-uuid")
    table.pin_root("root-uuid")
    assert table.key_for(ROOT_INODE) == "root-uuid"
    assert len(table) == 1


def test_a_new_root_displaces_the_old_one(table):
    """A refresh can hand back a different root — an impersonated view, or a
    caller whose visible tree starts somewhere else. The old key must not be
    left pointing at inode 1."""
    table.pin_root("first")
    table.pin_root("second")
    assert table.key_for(ROOT_INODE) == "second"
    assert table.inode_for("second") == ROOT_INODE
    assert table.inode_for("first") != ROOT_INODE


def test_the_same_key_always_gets_the_same_inode(table):
    first = table.inode_for("doc-a")
    assert table.inode_for("doc-a") == first
    assert table.inode_for("doc-a") == first


def test_different_keys_get_different_inodes(table):
    assert table.inode_for("doc-a") != table.inode_for("doc-b")


def test_inodes_are_never_the_root_by_accident(table):
    """Allocation starts above 1, because handing an ordinary document inode 1
    would make it the mount point."""
    assert all(table.inode_for(f"doc-{i}") > ROOT_INODE for i in range(20))


def test_an_inode_nobody_allocated_has_no_key(table):
    assert table.key_for(9999) is None


def test_an_entry_survives_until_the_kernel_has_given_it_all_back(table):
    """readdir, lookup and getattr can each hand out the same entry, and the
    kernel returns the total in one forget. Releasing on the first would leave
    an open file handle pointing at nothing."""
    inode = table.inode_for("doc-a")
    table.remember(inode)
    table.remember(inode)
    table.remember(inode)

    table.forget(inode, 2)
    assert table.key_for(inode) == "doc-a"

    table.forget(inode, 1)
    assert table.key_for(inode) is None


def test_forgetting_takes_the_key_with_it(table):
    """Both directions, or the next lookup of the same path finds a stale
    inode with no entry behind it."""
    inode = table.inode_for("doc-a")
    table.remember(inode)
    table.forget(inode, 1)
    assert table.key_for(inode) is None
    assert table.inode_for("doc-a") != inode


def test_a_count_larger_than_the_outstanding_one_still_releases(table):
    inode = table.inode_for("doc-a")
    table.remember(inode)
    table.forget(inode, 5)
    assert table.key_for(inode) is None


def test_remembering_several_at_once(table):
    """pyfuse3 hands back a count, not a repetition."""
    inode = table.inode_for("doc-a")
    table.remember(inode, 4)
    table.forget(inode, 3)
    assert table.key_for(inode) == "doc-a"
    table.forget(inode, 1)
    assert table.key_for(inode) is None


def test_the_root_is_never_forgotten(table):
    """The kernel does send forget for inode 1 at unmount, and dropping it
    would take the mount point out from under everything still running."""
    table.pin_root("root-uuid")
    table.remember(ROOT_INODE, 3)
    table.forget(ROOT_INODE, 3)
    table.forget(ROOT_INODE, 1000)
    assert table.key_for(ROOT_INODE) == "root-uuid"


def test_forgetting_something_never_remembered_is_not_an_error(table):
    """This is the shape of bug that kills a filesystem rather than failing a
    syscall: an exception out of a FUSE handler comes out of pyfuse3.main()
    and takes the whole mount down. forget() is called with whatever the
    kernel believes, including for an inode this process handed out before a
    reconnect, so it must not raise."""
    inode = table.inode_for("doc-a")
    table.forget(inode, 1)
    table.forget(inode, 1)
    table.forget(12345, 1)


def test_a_scratch_file_renamed_onto_a_real_slug_keeps_its_inode(table):
    """The one case rekey exists for. vim writes `.guide.md.swp`, then renames
    it over `guide.md`; the file the editor holds open must stay the same
    file, so the inode cannot be reallocated even though the key changed."""
    inode = table.inode_for("scratch:.guide.md.swp")
    table.remember(inode)

    table.rekey("scratch:.guide.md.swp", "draft:root.guide")

    assert table.inode_for("draft:root.guide") == inode
    assert table.key_for(inode) == "draft:root.guide"
    assert table.inode_for("scratch:.guide.md.swp") != inode


def test_rekeying_something_absent_does_nothing(table):
    table.rekey("never-existed", "somewhere")
    assert table.key_for(ROOT_INODE) is None
    assert len(table) == 0


def test_the_table_does_not_grow_without_bound(table):
    """The leak that matters: a mount that lists a large tree repeatedly must
    settle, not accumulate an entry per listing."""
    for _ in range(5):
        inodes = [table.inode_for(f"doc-{i}") for i in range(50)]
        for inode in inodes:
            table.remember(inode)
    assert len(table) == 50
    for i in range(50):
        table.forget(table.inode_for(f"doc-{i}"), 5)
    assert len(table) == 0
