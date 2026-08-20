"""The mount, exercised the way a person and an editor would.

Run this inside a private mount namespace if your environment needs one:

    unshare --user --map-root-user --mount --propagation private \\
        nix run --file . tests -- test/test_mount.py

Every test here shares one mount, because mounting costs a manifest fetch and a
FUSE handshake and nothing a test does cannot be undone by clearing drafts.
Isolation comes from the `clean` fixture, not from remounting.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

from conftest import wait_for

pytestmark = pytest.mark.mount
no_macos_xattrs = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="the FUSE-T NFS and FSKit transports do not expose xattrs",
)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def drafts(stack) -> int:
    return stack.count("select count(*) from wiki.draft")


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

def test_a_readable_document_is_a_file(mount, clean):
    assert (mount / "public/welcome.md").is_file()


def test_a_document_that_may_not_be_mirrored_is_absent(mount, clean):
    """bob may read secret-plans and may not sync it. The mount is a copy, so
    absent is the honest answer — and it is the same answer as "no such page",
    which is what stops the tree from leaking a name."""
    assert not (mount / "engineering/secret-plans.md").exists()


def test_a_document_with_no_published_revision_stats_as_empty(mount, clean):
    assert (mount / "public/unpublished.md").stat().st_size == 0


def test_size_matches_the_bytes_that_come_out(mount, clean):
    path = mount / "public/welcome.md"
    assert path.stat().st_size == len(path.read_bytes())


# ---------------------------------------------------------------------------
# Mode bits follow capabilities
# ---------------------------------------------------------------------------

def test_mode_bits_follow_capabilities(mount, clean):
    """The affordance, not the enforcement. The server decides; this is so an
    editor knows before the user has typed a paragraph."""
    assert mode(mount / "public/welcome.md") == 0o444        # bob only reads here
    assert mode(mount / "engineering/onboarding.md") == 0o644  # and edits here


def test_writing_where_the_acl_forbids_it_fails(mount, clean):
    with pytest.raises(OSError):
        (mount / "public/welcome.md").write_text("x")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_an_in_place_write_becomes_a_draft(mount, clean):
    (mount / "engineering/onboarding.md").write_text("# Onboarding\n\nedited in place\n")
    assert wait_for(lambda: drafts(clean) == 1, what="the write to become a draft")
    assert "edited in place" in (mount / "engineering/onboarding.md").read_text()


def test_an_atomic_save_lands_on_the_target(mount, clean):
    """The rename dance vim, emacs and VS Code all do. The scratch name is not a
    legal slug, which is why it can exist locally and never reaches the server."""
    scratch = mount / "engineering/.onboarding.md.swp"
    scratch.write_text("# Onboarding\n\nsaved atomically\n")
    scratch.rename(mount / "engineering/onboarding.md")

    assert "saved atomically" in (mount / "engineering/onboarding.md").read_text()
    assert not scratch.exists()
    assert wait_for(lambda: drafts(clean) == 1, what="the atomic save to become a draft")


def test_creating_a_page(mount, clean):
    (mount / "engineering/notes.md").write_text("# Notes\n")
    assert "# Notes" in (mount / "engineering/notes.md").read_text()
    assert wait_for(lambda: clean.count(
        "select count(*) from wiki.draft where path = 'root.engineering.notes'") == 1,
        what="the create to reach the server")


def test_removing_a_draft_page_takes_it_out_of_the_tree(mount, clean):
    page = mount / "engineering/notes.md"
    page.write_text("# Notes\n")
    wait_for(lambda: drafts(clean) == 1, what="the draft")
    page.unlink()
    assert not page.exists()
    assert wait_for(lambda: drafts(clean) == 0, what="the draft to go")


def test_directories_are_local(mount, clean):
    """A folder with nothing in it is not a document, so it never reaches the
    server — but an editor that makes one mid-save must not fail."""
    scratch = mount / "engineering/scratchdir"
    scratch.mkdir()
    assert scratch.is_dir()
    scratch.rmdir()
    assert not scratch.exists()
    assert drafts(clean) == 0


# ---------------------------------------------------------------------------
# What the mount tells the world about itself
# ---------------------------------------------------------------------------

def xattr(path, name: str) -> str:
    out = subprocess.run(["getfattr", "-n", f"user.fswiki.{name}", "--only-values",
                          str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@no_macos_xattrs
def test_state_is_exposed_as_an_xattr(mount, clean):
    """So a shell prompt or a status line can see it without asking the server."""
    page = mount / "engineering/onboarding.md"
    assert xattr(page, "state") == "published"
    page.write_text("probe\n")
    wait_for(lambda: drafts(clean) == 1, what="the draft")
    assert xattr(page, "state") == "draft"


@no_macos_xattrs
def test_the_ltree_path_is_exposed_as_an_xattr(mount, clean):
    """Which is how the CLI resolves a mount path without reimplementing the
    naming rules — see the `fswiki diff <path-in-the-mount>` test."""
    assert xattr(mount / "engineering/onboarding.md", "path") == "root.engineering.onboarding"


@no_macos_xattrs
def test_capabilities_are_exposed_as_an_xattr(mount, clean):
    caps = xattr(mount / "engineering/onboarding.md", "capabilities").split(",")
    assert "read" in caps and "write" in caps
    assert "read" in xattr(mount / "public/welcome.md", "capabilities")
    assert "write" not in xattr(mount / "public/welcome.md", "capabilities")


@no_macos_xattrs
def test_a_local_only_file_says_so(mount, clean):
    """A scratch name cannot be a slug, so it never reaches the server. Saying
    that in the xattr is the only way the mount can tell anyone."""
    scratch = mount / "engineering/.probe.swp"
    scratch.write_text("x")
    try:
        assert "local only" in xattr(scratch, "state")
    finally:
        scratch.unlink()


def test_an_ordinary_mount_is_writable_to_the_kernel(mount, clean):
    out = subprocess.run(["findmnt", "-n", "-o", "OPTIONS", "--target", str(mount.path)],
                         capture_output=True, text=True)
    assert out.stdout.strip().split(",")[0] == "rw"


# ---------------------------------------------------------------------------
# Retiring, moving, truncating
# ---------------------------------------------------------------------------

TARGET = "engineering/onboarding.md"
LTREE = "root.engineering.onboarding"


@pytest.fixture(scope="module")
def retirer(mount_factory):
    """A mount as frank, who holds `delete` on `io-test` and nothing else.

    The fixtures give him a custom role with exactly one capability, over a
    subtree whose own ACE is inherit-only — so his access to `io-test/child.md`
    is read (from `everyone`, inherited) plus delete, and no write at all. That
    combination is the point: retiring a page and editing it are different
    permissions, and a client that conflated them would let him do neither or
    both.
    """
    return mount_factory(user="frank")


def restore(mount, clean, path):
    """Drop whatever draft this test made and wait for the tree to agree.

    These change the shape of the tree rather than the contents of one file, so
    leaving the draft behind hands the next test a mount with a file missing or
    in the wrong place. `clean` empties drafts before each test, but it cannot
    make a mount notice, and a poll interval is not a thing to race.
    """
    clean.exec("delete from wiki.draft")
    wait_for(lambda: (mount / path).exists(), what=f"{path} to come back")


def test_removing_a_published_page_retires_it_rather_than_deleting_it(
        retirer, clean):
    """`rm` is a *proposal*. The page stays published until the draft is
    pushed, which is what lets `fswiki revert` put it back — and what stops a
    stray `rm -rf` in a working copy from being a publish."""
    page = retirer / "io-test/child.md"
    assert page.exists()
    page.unlink()
    try:
        assert wait_for(lambda: clean.count(
            "select count(*) from wiki.draft where operation = 'delete' "
            "and path = 'root.io-test.child'") == 1,
            what="the retirement to reach the server")
        assert not page.exists()
        assert clean.content("root.io-test.child", user="frank"), \
            "the page is still published until somebody pushes"
    finally:
        restore(retirer, clean, "io-test/child.md")


def test_retiring_does_not_drag_writing_in_with_it(retirer, clean):
    """frank's role holds `delete` and nothing else. If the mount granted
    write alongside it, the ACL would be saying one thing and the filesystem
    another — and the user would find out at push time."""
    page = retirer / "io-test/child.md"
    assert mode(page) == 0o444
    with pytest.raises(OSError):
        page.write_text("frank was here\n")
    assert drafts(clean) == 0


def test_a_page_may_not_be_removed_without_the_capability(mount, clean):
    """bob is an editor on engineering: read, write, author — and no delete.
    The mount refuses before the server has to, because an editor that gets
    EACCES from unlink says something useful and one that gets a 403 three
    layers down does not."""
    with pytest.raises(OSError):
        (mount / TARGET).unlink()
    assert (mount / TARGET).exists()
    assert drafts(clean) == 0


def test_moving_a_published_page_records_where_it_is_going(mount, clean):
    """And shows it there immediately. Anything else means `mv` appears to
    have done nothing until you push."""
    page = mount / TARGET
    moved = mount / "engineering/renamed.md"
    page.rename(moved)
    try:
        assert wait_for(lambda: clean.count(
            "select count(*) from wiki.draft where operation = 'move' "
            "and path = 'root.engineering.renamed'") == 1,
            what="the move to reach the server")
        assert moved.exists()
        assert not page.exists()
    finally:
        restore(mount, clean, TARGET)


def test_a_move_onto_an_occupied_name_is_refused(mount, clean):
    """EEXIST rather than a silent overwrite: the occupant is somebody else's
    published page, and "replace it" is not the ACL question the user thinks
    they are asking."""
    with pytest.raises(OSError):
        (mount / TARGET).rename(mount / "public/welcome.md")
    assert (mount / TARGET).exists()
    assert drafts(clean) == 0


def test_renaming_a_page_that_was_never_published_rewrites_it_in_place(
        mount, clean):
    """There is no document to move — only a draft — so the create is rewritten
    at the new path and the old one dropped. Distinct from moving a published
    page, and distinct again from the scratch-file rename an editor does."""
    first = mount / "engineering/notes.md"
    first.write_text("# Notes\n")
    wait_for(lambda: clean.count(
        "select count(*) from wiki.draft where path = 'root.engineering.notes'") == 1,
        what="the create")

    first.rename(mount / "engineering/notes-renamed.md")
    assert wait_for(lambda: clean.count(
        "select count(*) from wiki.draft "
        "where path = 'root.engineering.notes-renamed'") == 1,
        what="the draft to move")
    assert clean.count(
        "select count(*) from wiki.draft where path = 'root.engineering.notes'") == 0
    assert (mount / "engineering/notes-renamed.md").read_text() == "# Notes\n"


def test_a_renamed_draft_is_readable_immediately(mount):
    """The same rename, read back with nothing in between.

    The test above waits on the database first, and that wait is what used to
    make it pass: the kernel's attribute TTL expired while it polled, so the
    read went through a fresh lookup and never noticed that the inode was
    stale. Reading straight away is the assertion that actually pins the rekey
    -- after a rename the kernel keeps the *source* inode and files it under
    the new name, so that inode has to start resolving to the new draft.

    It surfaced when the ACL stopped costing ~90 ms per manifest fetch. A
    latent bug that only a slow server was hiding is worth a test that does not
    depend on how fast the server is.
    """
    original = mount / "engineering/immediate.md"
    original.write_text("# Immediate\n")
    original.rename(mount / "engineering/immediate-renamed.md")
    assert (mount / "engineering/immediate-renamed.md").read_text() == "# Immediate\n"


def test_truncating_a_file_shortens_it(mount, clean):
    """`> file` in a shell, and what several editors do before writing. It
    arrives as a setattr carrying a size and no data at all, so a mount that
    implements only write silently ignores it and the file keeps its old
    tail."""
    page = mount / TARGET
    original = page.read_bytes()
    assert len(original) > 4
    with open(page, "r+") as fh:
        fh.truncate(4)
    assert wait_for(lambda: page.stat().st_size == 4, what="the truncation")
    assert page.read_bytes() == original[:4]
    assert wait_for(lambda: drafts(clean) == 1,
                    what="the truncation to become a draft")


def test_truncating_to_nothing_leaves_an_empty_file_not_a_missing_one(
        mount, clean):
    page = mount / TARGET
    with open(page, "w"):
        pass
    assert wait_for(lambda: page.stat().st_size == 0, what="the emptying")
    assert page.exists()


def test_the_filesystem_answers_df(mount, clean):
    """`df` runs on anything mounted, including from a file manager's status
    bar. There is no block device behind this, so a filesystem of no size is
    the honest answer — but it has to be an answer, because statvfs failing
    makes tools report the mount as broken."""
    info = os.statvfs(str(mount.path))
    assert info.f_namemax == 255, "the kernel's NAME_MAX, and the slug limit"
    assert info.f_bsize > 0
    assert info.f_files >= 1, "one entry per thing in the tree"


# ---------------------------------------------------------------------------
# The parts a shell reaches for
# ---------------------------------------------------------------------------

def test_dot_dot_walks_back_up(mount, clean):
    """`cd ..` and every relative path that goes through a parent. FUSE asks
    for it by name like any other lookup, and a filesystem that does not
    answer makes `..` an ENOENT from inside its own directories."""
    assert (mount / "engineering/../public/welcome.md").is_file()
    assert (mount / "engineering/./onboarding.md").is_file()
    # Two levels down, so the answer comes from the tree rather than from the
    # root's own shortcut.
    assert (mount / "engineering/private/../onboarding.md").is_file()


@no_macos_xattrs
def test_getfattr_dumps_every_attribute(mount, clean):
    """`getfattr -d`, which is what someone types before they know the names.
    Every other test here asks for one attribute by name and so never
    exercises the listing at all."""
    out = subprocess.run(["getfattr", "-d", "-m", "-", str(mount / TARGET)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for name in ("path", "capabilities", "state", "document_id", "version"):
        assert f"user.fswiki.{name}=" in out.stdout, out.stdout


@no_macos_xattrs
def test_a_scratch_file_advertises_only_that_it_is_scratch(mount, clean):
    """It has no server-side existence, so there is no path, no capability set
    and no revision to report — and inventing any of them would be a lie a
    status line would print."""
    scratch = mount / "engineering/.probe.swp"
    scratch.write_text("x")
    try:
        out = subprocess.run(["getfattr", "-d", "-m", "-", str(scratch)],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert "state=" in out.stdout
        assert "user.fswiki.path=" not in out.stdout
        assert "user.fswiki.version=" not in out.stdout
    finally:
        scratch.unlink()


@no_macos_xattrs
def test_an_attribute_that_is_not_ours_is_simply_absent(mount, clean):
    """Not an error the caller has to special-case: ENOATTR is what every
    filesystem says, and tools like `cp -a` ask for attributes nobody has."""
    for name in ("user.fswiki.nonesuch", "user.something.else", "security.selinux"):
        out = subprocess.run(["getfattr", "-n", name, str(mount / TARGET)],
                             capture_output=True, text=True)
        assert out.returncode != 0, f"{name} should not exist"


@no_macos_xattrs
def test_the_acl_is_not_administered_through_xattrs(mount, clean):
    """Refusing plainly beats accepting and discarding. Granting somebody
    `write` needs a grammar and an audit trail, and `setfattr` has neither —
    a mount that silently ignored the write would let an admin believe they
    had made a change."""
    out = subprocess.run(
        ["setfattr", "-n", "user.fswiki.capabilities", "-v", "read,write",
         str(mount / TARGET)], capture_output=True, text=True)
    assert out.returncode != 0
    assert "delete" not in xattr(mount / TARGET, "capabilities")


def test_a_scratch_file_can_be_renamed_to_another_scratch_name(mount, clean):
    """Editors shuffle their own temporary files around before landing one.
    Neither name is a legal slug, so the whole exchange is local and the
    server never hears about it."""
    first = mount / "engineering/.probe.swp"
    second = mount / "engineering/.probe.swpx"
    first.write_text("interim\n")
    try:
        first.rename(second)
        assert second.read_text() == "interim\n"
        assert not first.exists()
        assert drafts(clean) == 0
    finally:
        for path in (first, second):
            if path.exists():
                path.unlink()
