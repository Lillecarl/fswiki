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

import pytest

from conftest import wait_for

pytestmark = pytest.mark.mount


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


def test_state_is_exposed_as_an_xattr(mount, clean):
    """So a shell prompt or a status line can see it without asking the server."""
    page = mount / "engineering/onboarding.md"
    assert xattr(page, "state") == "published"
    page.write_text("probe\n")
    wait_for(lambda: drafts(clean) == 1, what="the draft")
    assert xattr(page, "state") == "draft"


def test_the_ltree_path_is_exposed_as_an_xattr(mount, clean):
    """Which is how the CLI resolves a mount path without reimplementing the
    naming rules — see the `fswiki diff <path-in-the-mount>` test."""
    assert xattr(mount / "engineering/onboarding.md", "path") == "root.engineering.onboarding"


def test_capabilities_are_exposed_as_an_xattr(mount, clean):
    caps = xattr(mount / "engineering/onboarding.md", "capabilities").split(",")
    assert "read" in caps and "write" in caps
    assert "read" in xattr(mount / "public/welcome.md", "capabilities")
    assert "write" not in xattr(mount / "public/welcome.md", "capabilities")


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
