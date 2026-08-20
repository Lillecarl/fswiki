"""A picture in the mount, which is the point of making a file a revision.

`cp diagram.png ~/wiki/public/` has to do what `cp notes.md` does: become a
draft, show up in `fswiki status`, survive a `push`, and come back byte for
byte. Anything less and "filesystem first" has an exception in it that you
cannot see from the filesystem.

The assertions worth reading twice are the round trips. A binary body used to
go through `data.decode("utf-8", errors="surrogateescape")` on its way to a
draft, which produces lone surrogates that neither UTF-8 nor JSON can carry
back out -- so a picture written through the text column arrived corrupted or
not at all. Every test here that compares bytes is testing that the column
split reaches all the way from the kernel to Postgres and back.
"""

from __future__ import annotations

import pytest

from conftest import wait_for

pytestmark = pytest.mark.mount

# A real 1x1 PNG, and a body that is not valid UTF-8 anywhere in it.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100fdff03fa0000000049454e44ae426082")

# Every byte value there is. The one input that finds an encoding bug wherever
# it is hiding: 0x00 breaks C strings, 0x80-0xFF are invalid UTF-8 on their
# own, and 0xED 0xA0 0x80 is a lone surrogate.
EVERY_BYTE = bytes(range(256))


def drafts(stack) -> int:
    return stack.count("select count(*) from wiki.draft")


@pytest.fixture
def put(mount, clean, stack):
    """Write a file through the mount and wait for its draft to land."""
    def do(rel: str, data: bytes) -> None:
        before = drafts(stack)

        def written():
            try:
                (mount / rel).write_bytes(data)
                return True
            except OSError:
                # `clean` empties the draft table behind the mount's back, so
                # for up to one poll the tree still lists a file whose draft is
                # gone. Retrying is the honest wait.
                return False

        wait_for(written, what=f"the mount to accept {rel}")
        wait_for(lambda: drafts(stack) > before, what=f"the draft for {rel}")
    return do


# ---------------------------------------------------------------------------
# Writing one
# ---------------------------------------------------------------------------

def test_a_picture_written_through_the_mount_becomes_a_draft(put, mount, stack):
    put("engineering/diagram.png", PNG)
    assert stack.psql(
        "select octet_length(content_bytes) from wiki.draft "
        "where path = 'root.engineering.diagram'") == str(len(PNG))


def test_it_goes_in_the_bytes_column_and_not_the_text_one(put, stack):
    """The whole of the change, in one assertion. A body in `content` would be
    a picture that has been through a UTF-8 decode."""
    put("engineering/diagram.png", PNG)
    assert stack.psql("select content is null from wiki.draft "
                      "where path = 'root.engineering.diagram'") == "t"


def test_reading_it_back_gives_the_same_bytes(put, mount):
    put("engineering/diagram.png", PNG)
    assert (mount / "engineering/diagram.png").read_bytes() == PNG


def test_every_byte_value_survives_the_round_trip(put, mount):
    """0x00, every high byte, and a lone surrogate. If the body took the text
    path anywhere between the kernel and Postgres, this is what finds it."""
    put("engineering/all-bytes.png", EVERY_BYTE)
    assert (mount / "engineering/all-bytes.png").read_bytes() == EVERY_BYTE


def test_the_size_is_the_size(put, mount):
    put("engineering/diagram.png", PNG)
    assert (mount / "engineering/diagram.png").stat().st_size == len(PNG)


def test_the_content_type_comes_from_the_extension(put, stack):
    put("engineering/diagram.png", PNG)
    assert stack.psql("select content_type from wiki.draft "
                      "where path = 'root.engineering.diagram'") == "image/png"


def test_a_name_the_wiki_cannot_hold_is_still_scratch(mount, clean, stack):
    """An editor writing `.diagram.png.swp` beside the file must not create a
    document. The extension maps merged; the slug rules did not."""
    before = drafts(stack)
    scratch = mount / "engineering/report.tar.gz"
    scratch.write_bytes(b"not a wiki file")
    try:
        assert scratch.read_bytes() == b"not a wiki file"
        assert drafts(stack) == before
    finally:
        # Scratch lives in the mount process, not in the database, so `clean`
        # cannot reach it and it would still be in the listing when the next
        # test compares two mounts file for file.
        scratch.unlink()


# ---------------------------------------------------------------------------
# Publishing one
# ---------------------------------------------------------------------------

def test_status_shows_it_as_a_file(put, cli):
    put("engineering/diagram.png", PNG)
    r = cli("status", user="bob")
    assert r.code == 0, r.out
    assert "engineering/diagram" in r.out


def test_diff_reports_bytes_rather_than_attempting_a_text_diff(put, cli):
    """A unified diff of two pictures is noise. The size is the thing a person
    can act on."""
    put("engineering/diagram.png", PNG)
    r = cli("diff", user="bob")
    assert r.code == 0, r.out
    assert "a file" in r.out and str(len(PNG)) in r.out
    assert "@@" not in r.out


def test_push_publishes_it_and_it_reads_back_the_same(put, mount, cli, stack):
    put("engineering/pushed.png", PNG)
    r = cli("push", "-m", "a picture", user="bob")
    assert r.code == 0, r.out

    assert stack.psql(
        "select octet_length(v.content_bytes) from wiki.document_version v "
        "join wiki.document d on d.id = v.document_id "
        "where d.path = 'root.engineering.pushed' and upper_inf(v.valid)"
    ) == str(len(PNG))

    wait_for(lambda: (mount / "engineering/pushed.png").exists(),
             what="the published picture")
    assert (mount / "engineering/pushed.png").read_bytes() == PNG


def test_a_published_picture_is_in_the_tree_a_mirror_sees(put, cli, mount, stack):
    """`syncable_document` used to exclude attachments, because they had no
    revision and a mirror would have written a zero-byte file. They are
    revisions now, so the mount lists one like anything else."""
    put("engineering/mirrored.png", PNG)
    assert cli("push", "-m", "x", user="bob").code == 0
    wait_for(lambda: (mount / "engineering/mirrored.png").exists(),
             what="the file in the tree")
    assert (mount / "engineering/mirrored.png").stat().st_size == len(PNG)


def test_replacing_a_published_picture_is_a_second_revision(put, cli, mount, stack):
    put("engineering/twice.png", PNG)
    assert cli("push", "-m", "one", user="bob").code == 0
    wait_for(lambda: (mount / "engineering/twice.png").exists(), what="revision 1")

    put("engineering/twice.png", EVERY_BYTE)
    assert cli("push", "-m", "two", user="bob").code == 0

    assert stack.psql(
        "select count(*) from wiki.document_version v "
        "join wiki.document d on d.id = v.document_id "
        "where d.path = 'root.engineering.twice'") == "2"
    wait_for(lambda: (mount / "engineering/twice.png").stat().st_size
             == len(EVERY_BYTE), what="revision 2")
    assert (mount / "engineering/twice.png").read_bytes() == EVERY_BYTE
    # And the first revision still holds what it held.
    assert bytes.fromhex(stack.psql(
        "select encode(v.content_bytes, 'hex') from wiki.document_version v "
        "join wiki.document d on d.id = v.document_id "
        "where d.path = 'root.engineering.twice' and v.version = 1")) == PNG


# ---------------------------------------------------------------------------
# Removing one
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def owner(mount_factory):
    """A mount as alice, who holds `delete` as well as `write`.

    bob can write in `engineering` and cannot retire anything there, which is
    the ACL working -- so removing a file needs somebody who may.
    """
    return mount_factory(user="alice")


def test_deleting_it_through_the_mount_is_a_retirement(owner, cli, clean, stack):
    path = owner / "engineering/doomed.png"

    def written():
        try:
            path.write_bytes(PNG)
            return True
        except OSError:
            return False

    wait_for(written, what="the mount to accept the picture")
    wait_for(lambda: drafts(stack) > 0, what="the draft")
    assert cli("push", "-m", "x", user="alice").code == 0
    wait_for(path.exists, what="the file")

    path.unlink()
    wait_for(lambda: stack.count(
        "select count(*) from wiki.draft where operation = 'delete'") == 1,
        what="the retirement draft")
    assert cli("push", "-m", "gone", user="alice").code == 0

    assert stack.psql(
        "select v.is_tombstone from wiki.document_version v "
        "join wiki.document d on d.id = v.document_id "
        "where d.path = 'root.engineering.doomed' and upper_inf(v.valid)") == "t"
    # The bytes are still in history, which is what makes this a retirement.
    assert stack.psql(
        "select count(*) from wiki.document_version v "
        "join wiki.document d on d.id = v.document_id "
        "where d.path = 'root.engineering.doomed' "
        "and v.content_bytes is not null") == "1"


# ---------------------------------------------------------------------------
# What cannot be done to bytes
# ---------------------------------------------------------------------------

def test_render_refuses_a_file(put, cli):
    """`fswiki render` turns markup into HTML. A PNG is not markup, and saying
    so beats handing mojibake to a renderer."""
    put("engineering/diagram.png", PNG)
    r = cli("render", "engineering/diagram.png", user="bob")
    assert r.code == 1
    assert "not a page" in r.out


def test_revert_reports_it_in_bytes(put, cli, mount):
    put("engineering/diagram.png", PNG)
    r = cli("revert", user="bob")
    assert r.code == 0, r.out
    assert "bytes" in r.out


def test_revert_apply_takes_it_away(put, cli, mount, stack):
    put("engineering/undone.png", PNG)
    assert cli("revert", "--apply", user="bob").code == 0
    assert stack.count("select count(*) from wiki.draft") == 0
    wait_for(lambda: not (mount / "engineering/undone.png").exists(),
             what="the file to go")
