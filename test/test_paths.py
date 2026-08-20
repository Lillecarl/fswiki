"""Turning what someone typed into the path the server wants.

Every CLI command takes paths from a human, and a human has three ways of
naming the same document: the file in their mount, the ltree path the server
prints, and whatever their shell completed. Getting this wrong is not a crash
— it is `fswiki push docs/guide.md` quietly pushing nothing because the path
resolved to something that does not exist.

Pure functions and one temporary file: no stack, no network.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from fswiki_cli import paths

no_macos_xattrs = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="the FUSE-T NFS and FSKit transports do not expose xattrs",
)


@pytest.mark.parametrize("typed", [
    "public/welcome.md",
    "public/welcome",
    "/public/welcome.md",
    "./public/welcome.md",
    "root/public/welcome.md",
])
def test_the_ways_a_shell_hands_you_one_path(typed):
    assert paths.from_filesystem(typed) == "root.public.welcome"


def test_a_bare_name_is_a_document_at_the_root():
    assert paths.from_filesystem("welcome.md") == "root.welcome"


def test_nothing_resolves_to_the_root():
    assert paths.from_filesystem("") == "root"
    assert paths.from_filesystem("/") == "root"
    assert paths.from_filesystem("root") == "root"


def test_a_leaf_the_wiki_could_not_hold_is_refused_by_name():
    """PathError rather than a bare ValueError, so __main__ can tell "you typed
    something impossible" from "something else went wrong" and print the one
    that helps."""
    with pytest.raises(paths.PathError, match="not a name the wiki can hold"):
        paths.from_filesystem("docs/report.tar.gz")


def test_a_folder_that_is_not_a_slug_says_so_about_the_folder():
    with pytest.raises(paths.PathError, match="not a valid folder name"):
        paths.from_filesystem("my docs/guide.md")


def test_an_ltree_path_is_recognised_and_passed_through():
    """Because it is what every error message and every report prints, so it
    is what people copy back in."""
    assert paths.looks_like_ltree("root.public.welcome")
    assert paths.looks_like_ltree("root")
    assert paths.resolve("root.public.welcome") == "root.public.welcome"


@pytest.mark.parametrize("value", ["public.welcome", "rootless", "welcome.md", ""])
def test_things_that_only_look_like_ltree(value):
    """`welcome.md` contains a dot and is still a filename. Only a `root`
    prefix makes it the other thing."""
    assert not paths.looks_like_ltree(value)


def test_resolve_falls_through_to_the_filesystem_reading():
    assert paths.resolve("public/welcome.md") == "root.public.welcome"


def test_a_path_inside_a_marked_mount_finds_its_path_and_server(tmp_path):
    mount = tmp_path / "mount with spaces"
    page = mount / "public" / "welcome.md"
    page.parent.mkdir(parents=True)
    page.write_text("hello")
    (mount / ".fswiki").write_text(json.dumps({
        "format": "fswiki-mount", "version": 1, "url": "https://wiki.example",
    }))

    found = paths.from_mount(str(page))
    assert found is not None
    assert found.path == "root.public.welcome"
    assert found.url == "https://wiki.example"
    assert found.root == mount
    assert paths.resolve(str(page)) == "root.public.welcome"


def test_an_unrelated_dot_fswiki_file_is_not_a_mount_marker(tmp_path):
    page = tmp_path / "welcome.md"
    page.write_text("hello")
    (tmp_path / ".fswiki").write_text("not fswiki metadata")
    assert paths.from_mount(str(page)) is None


def test_a_path_that_is_not_a_file_is_not_consulted_for_xattrs():
    """from_xattr runs first on every input, including inputs that were never
    meant to be filenames, so a missing file must be silent."""
    assert paths.from_xattr("/nonexistent/definitely/not/here") is None
    assert paths.from_xattr("root.public.welcome") is None


@no_macos_xattrs
def test_a_real_file_beats_every_guess(tmp_path):
    """The mount records the exact path on the file, and a recorded answer is
    the only one that is certainly right — it survives the mount being
    somewhere the CLI would never have guessed."""
    f = tmp_path / "anything-at-all.md"
    f.write_text("x")
    try:
        os.setxattr(f, paths.XATTR_PATH, b"root.somewhere.else")
    except (OSError, AttributeError) as exc:
        pytest.skip(f"no user xattrs on {tmp_path}: {exc}")
    assert paths.from_xattr(str(f)) == "root.somewhere.else"
    # And it wins: the filename would have said `root.anything-at-all`.
    assert paths.resolve(str(f)) == "root.somewhere.else"


@no_macos_xattrs
def test_an_empty_xattr_is_treated_as_absent(tmp_path):
    f = tmp_path / "guide.md"
    f.write_text("x")
    try:
        os.setxattr(f, paths.XATTR_PATH, b"")
    except (OSError, AttributeError) as exc:
        pytest.skip(f"no user xattrs on {tmp_path}: {exc}")
    assert paths.from_xattr(str(f)) is None


@pytest.mark.parametrize("path,shown", [
    ("root.public.welcome", "public/welcome"),
    ("root.welcome", "welcome"),
    ("root", "/"),
])
def test_what_a_report_prints(path, shown):
    """Without an extension, deliberately: the content type is not part of the
    path, and a report that guessed `.md` would be wrong for every other kind
    of document."""
    assert paths.to_display(path) == shown


def test_display_and_back_is_a_round_trip():
    for path in ("root.public.welcome", "root.a.b.c.d", "root.welcome"):
        assert paths.from_filesystem(paths.to_display(path)) == path
