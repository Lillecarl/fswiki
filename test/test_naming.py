"""The mapping between filenames and slugs, in both directions.

This is the smallest module in the project and the one everything else assumes.
A slug can never contain a dot, and that single constraint is what makes
`guide.md` decompose exactly rather than heuristically. Every other layer —
the mount's directory listing, the CLI's path argument, a wikilink's target —
is built on that being true, so it is worth checking directly rather than
inferring from the fact that a mount happened to list the right names.

Pure functions: no stack, no network.
"""

from __future__ import annotations

import pytest

from fswiki_core import naming

EXTENSIONS = sorted(naming.TYPE_BY_EXT)


def test_an_ordinary_word_is_a_slug():
    assert naming.is_slug("welcome")


@pytest.mark.parametrize("value", ["a.b", "a/b", "a\\b", "a b", "a\tb", "a\nb", ""])
def test_the_characters_the_server_refuses(value):
    """Mirrors wiki.document_slug_shape. If this drifts, push starts failing
    server-side for names the client happily created."""
    assert not naming.is_slug(value)


def test_length_is_counted_in_bytes_not_characters():
    """NAME_MAX is 255 bytes and so is the column. A CJK name three bytes to
    the character is over the limit at 86 characters, and a client that
    measured `len()` would offer the user a filename the kernel cannot store."""
    assert naming.is_slug("a" * 255)
    assert not naming.is_slug("a" * 256)
    assert naming.is_slug("あ" * 85)
    assert not naming.is_slug("あ" * 86)


def test_unicode_that_is_not_whitespace_is_fine():
    assert naming.is_slug("naïve")
    assert naming.is_slug("日本語")
    assert naming.is_slug("emoji-🙂")


@pytest.mark.parametrize("ext", EXTENSIONS)
def test_every_extension_round_trips(ext):
    """The property that matters: parse and unparse are inverses for every
    type we claim to know. A one-way table would silently rename documents."""
    name = f"guide{ext}"
    parsed = naming.parse_filename(name)
    assert parsed is not None
    slug, content_type = parsed
    assert slug == "guide"
    assert naming.filename(slug, content_type, is_folder=False) == name


def test_a_name_with_no_extension_is_markdown():
    """`touch notes` should do the obvious thing rather than make a scratch
    file the user cannot push."""
    assert naming.parse_filename("notes") == ("notes", naming.DEFAULT_CONTENT_TYPE)


@pytest.mark.parametrize("name", [
    "report.tar.gz",   # partitions to the extension `tar.gz`, which is not one
    "notes.bak",
    ".hidden",         # empty slug
    "file.swp",        # vim
    "file.md~",        # emacs -- the extension is `md~`
    "",
])
def test_names_we_will_not_pretend_to_understand(name):
    """Scratch, not an error. Guessing a content type from an arbitrary suffix
    is how `report.tar.gz` becomes the slug `report` and the archive is lost."""
    assert naming.parse_filename(name) is None


def test_a_folder_has_no_extension():
    assert naming.filename("engineering", "text/markdown", is_folder=True) == "engineering"


def test_a_content_type_we_do_not_know_gets_no_extension():
    """Better a bare name than an invented suffix: the name still round-trips
    to markdown, and nothing claims to know what the bytes are."""
    assert naming.filename("data", "application/x-thing", is_folder=False) == "data"


def test_a_missing_content_type_is_markdown():
    assert naming.filename("guide", None, is_folder=False) == "guide.md"


def test_splitting_an_ltree_path_is_exact():
    assert naming.ltree_labels("root.a.b") == ["root", "a", "b"]
    assert naming.ltree_labels("root") == ["root"]


def test_the_root_has_no_parent():
    assert naming.ltree_parent("root.a.b") == "root.a"
    assert naming.ltree_parent("root.a") == "root"
    assert naming.ltree_parent("root") is None


@pytest.mark.parametrize("display", [
    "public/welcome",
    "root/public/welcome",
    "/public/welcome",
    "public/welcome.md",
    "./public/welcome",
    "public\\welcome",
])
def test_every_way_of_writing_one_path_means_the_same_path(display):
    """A wikilink is written by a human, and humans write paths the way their
    shell completed them."""
    assert naming.from_display(display) == "root.public.welcome"


def test_an_empty_display_path_is_the_root():
    assert naming.from_display("") == "root"
    assert naming.from_display("root") == "root"


@pytest.mark.parametrize("display", [
    "public/report.tar.gz",   # unparseable leaf
    "pub lic/welcome",        # whitespace in a folder
    "a.b/welcome",            # a dot anywhere but the leaf's extension
])
def test_a_name_the_wiki_could_never_hold_raises(display):
    """Rather than being coerced into something close. `links.py` catches this
    and leaves the link as literal text, which is the honest rendering of a
    target that cannot exist."""
    with pytest.raises(ValueError):
        naming.from_display(display)


def test_the_extension_tables_agree_with_each_other():
    """EXT_BY_TYPE is built with setdefault so the first entry wins; if a type
    ever gained a second extension, the reverse lookup must still land back on
    a type that maps to it."""
    for ext, content_type in naming.TYPE_BY_EXT.items():
        assert naming.TYPE_BY_EXT[naming.EXT_BY_TYPE[content_type]] == content_type
        assert ext.startswith(".")
