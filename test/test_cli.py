"""The CLI, driven the way it is used: edit through the mount, publish with it.

Every test here goes through the real binary and the real filesystem, because
argument parsing, exit codes and what lands on stderr are part of what a CLI is.
"""

from __future__ import annotations

import sys

import pytest

from conftest import wait_for

pytestmark = pytest.mark.mount
no_macos_xattrs = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="the FUSE-T NFS and FSKit transports do not expose xattrs",
)


@pytest.fixture
def edit(mount, clean):
    """Write through the mount and wait for the draft to reach the server.

    Every test that edits needs this, and every one that forgets the wait is a
    test that passes or fails on timing.
    """
    def do(rel: str, text: str) -> None:
        before = clean.count("select count(*) from wiki.draft")

        def written():
            try:
                (mount / rel).write_text(text)
                return True
            except OSError:
                # `clean` empties the draft table behind the mount's back, so
                # for up to one poll the tree still lists a file whose draft is
                # gone. Retrying is the honest wait: what we need is not a
                # duration, it is for the mount to have caught up.
                return False

        wait_for(written, what=f"the mount to accept a write to {rel}")
        wait_for(lambda: clean.count("select count(*) from wiki.draft") > before,
                 what=f"the edit to {rel} to become a draft")
    return do


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

def test_whoami(cli, clean):
    r = cli("whoami")
    assert r.code == 0 and clean.who("bob") in r


def test_show_prints_source_from_a_path_inside_the_mount(cli, mount, clean):
    expected = clean.content("root.public.welcome")
    r = cli("show", str(mount / "public/welcome.md"), FSWIKI_URL="http://127.0.0.1:1")
    assert r.code == 0
    assert r.out == expected


def test_status_with_nothing_pending(cli, clean):
    assert "Nothing pending" in cli("status")


@pytest.mark.parametrize("command", [
    ["whoami"], ["status"], ["diff"], ["push", "-m", "x"], ["revert"],
    ["merge"], ["render", "public/welcome.md"],
])
def test_an_unreachable_server_is_one_clear_error(cli, command):
    """Every subcommand, not just whoami. httpx.TransportError is not an
    OSError, so a command that reached its own request before the shared guard
    would print a traceback — which is the difference between "the server is
    down" and "the tool is broken", and the user cannot tell them apart."""
    r = cli(*command, FSWIKI_URL="http://127.0.0.1:1")
    assert r.code == 1
    assert "cannot reach" in r
    assert "Traceback" not in r


# ---------------------------------------------------------------------------
# status and diff
# ---------------------------------------------------------------------------

def test_status_lists_both_kinds_of_change(cli, edit):
    edit("engineering/onboarding.md", "# Onboarding\n\nedited via the mount\n")
    edit("engineering/runbook.md", "# Runbook\n\nfresh page\n")
    r = cli("status")
    assert "engineering/onboarding" in r
    assert "engineering/runbook" in r
    assert "modified" in r


def test_diff_shows_the_added_line(cli, edit):
    edit("engineering/onboarding.md", "# Onboarding\n\nedited via the mount\n")
    assert "+edited via the mount" in cli("diff")


@no_macos_xattrs
def test_a_path_inside_the_mount_resolves_through_the_xattr(cli, edit, mount):
    """The CLI does not reimplement the naming rules; it asks the mount."""
    edit("engineering/runbook.md", "# Runbook\n")
    assert "engineering/runbook" in cli("diff", str(mount / "engineering/runbook.md"))


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def test_a_dry_run_changes_nothing(cli, edit, clean):
    edit("engineering/onboarding.md", "# Onboarding\n\ndry run\n")
    assert cli("push", "-n", "-m", "x").code == 0
    assert clean.count("select count(*) from wiki.draft") == 1


@no_macos_xattrs
def test_push_can_take_a_subset(cli, edit, clean, mount):
    edit("engineering/onboarding.md", "# Onboarding\n\nedited\n")
    edit("engineering/runbook.md", "# Runbook\n\nfresh\n")
    r = cli("push", "-m", "add the runbook", str(mount / "engineering/runbook.md"))
    assert "Published 1 change" in r
    assert "engineering/onboarding" in cli("status")
    assert clean.count("select count(*) from wiki.draft") == 1


def test_push_publishes_the_rest(cli, edit, clean):
    edit("engineering/onboarding.md", "# Onboarding\n\nedited\n")
    assert "Published 1 change" in cli("push", "-m", "edit onboarding")
    assert "Nothing pending" in cli("status")
    assert clean.count("select count(*) from wiki.draft") == 0


def test_a_page_the_reader_cannot_write_is_refused_by_name(cli, clean):
    """Seeded, because the mount would refuse the write before a draft existed.
    What is under test is what push does with one that got there anyway."""
    clean.exec("""
        insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
          select p.id, 'update', d.id, d.path, 'nope', d.version
            from wiki.principal p, wiki.current_document d
           where p.name = 'bob' and d.path = 'root.public.welcome'""")
    r = cli("push", "-m", "x")
    assert "FORBIDDEN" in r
    assert "no write capability" in r
    assert clean.count("select count(*) from wiki.draft") == 1, "all or nothing"


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------

def test_revert_with_nothing_pending(cli, clean):
    assert cli("revert").out.strip() == "Nothing pending."


def test_revert_is_a_dry_run_by_default(cli, edit, clean):
    published = clean.content("root.engineering.onboarding")
    edit("engineering/onboarding.md", published + "AN EXTRA LINE\n")
    r = cli("revert")
    assert "would be withdrawn" in r
    # Counted against the published text, not the file size: what revert
    # destroys is the difference, and saying "40 lines" about a one-line edit
    # would be alarming and wrong.
    assert "1 changed line" in r
    assert "Nothing keeps a copy" in r
    assert clean.count("select count(*) from wiki.draft") == 1


def test_revert_apply_puts_the_file_back(cli, edit, mount, clean):
    # From the server, not from the mount: `clean` empties the draft table
    # behind the mount's back, so for up to one poll the file still shows a
    # draft body — and a baseline read then is the text revert is about to
    # throw away, which would make this assert the opposite of what it means.
    published = clean.content("root.engineering.onboarding")
    edit("engineering/onboarding.md", published + "AN EXTRA LINE\n")
    assert "Withdrew 1 change" in cli("revert", "--apply")
    assert clean.count("select count(*) from wiki.draft") == 0
    assert wait_for(
        lambda: (mount / "engineering/onboarding.md").read_text() == published,
        what="the mount to show the published revision again")


def test_reverting_a_create_is_reported_as_a_total_loss(cli, edit, mount, clean):
    edit("engineering/revert-probe.md", "draft only\n")
    assert "published nowhere else" in cli("revert", "engineering/revert-probe.md")
    cli("revert", "--apply", "engineering/revert-probe.md")
    assert wait_for(lambda: not (mount / "engineering/revert-probe.md").exists(),
                    what="the file to leave the tree")


def test_reverting_a_delete_cancels_the_retirement(cli, clean, mount):
    """Seeded: bob holds `delete` on nothing in the fixtures, so `rm` would be
    refused before a draft existed. How the draft arose is not what is tested."""
    clean.exec("""
        insert into wiki.draft (author_id, operation, document_id, path, base_version)
          select (select id from wiki.principal where name = 'bob'), 'delete',
                 d.id, d.path, d.version
            from wiki.current_document d where d.path = 'root.engineering.onboarding'""")
    r = cli("revert", "engineering/onboarding.md")
    assert "retirement is cancelled" in r
    # A delete discards no text, so a line count would be meaningless.
    assert "lines" not in r
    cli("revert", "--apply", "engineering/onboarding.md")
    assert clean.count("select count(*) from wiki.draft") == 0


def test_revert_takes_a_subset_and_leaves_the_rest(cli, edit, clean):
    edit("engineering/rv1.md", "one\n")
    edit("engineering/rv2.md", "two\n")
    cli("revert", "--apply", "engineering/rv1.md")
    assert clean.scalar("select path from wiki.draft") == "root.engineering.rv2"


def test_an_unknown_path_is_refused_before_anything_is_withdrawn(cli, edit, clean):
    edit("engineering/rv1.md", "one\n")
    r = cli("revert", "--apply", "engineering/not-a-thing.md")
    assert "no pending change" in r
    assert clean.count("select count(*) from wiki.draft") == 1


def test_an_outstanding_merge_is_pointed_at_the_route_that_keeps_the_work(cli, edit, clean):
    edit("engineering/rv2.md", "two\n")
    clean.exec("update wiki.draft set pre_merge_content = 'older', merged_from = 1")
    r = cli("revert")
    assert "merge is outstanding" in r
    assert "merge --abort" in r


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def test_list_backends_says_what_this_installation_has(cli):
    r = cli("render", "--list-backends")
    assert "markdown-it-py" in r and "text/plain" in r


def test_render_a_published_document(cli, clean):
    r = cli("render", "public/guide/permissions.md")
    assert "<h1>" in r and "<pre>" in r


def test_an_unknown_backend_is_refused_by_name(cli, clean):
    assert "no render backend named" in cli("render", "--backend", "no-such-thing",
                                            "public/welcome.md")


def test_what_you_cannot_see_you_cannot_render(cli, clean):
    """And the message is the same one a missing page gets."""
    assert "no document at" in cli("render", "engineering/secret-plans.md")


LINKS = """# Links

Visible: [[public/welcome]]

Hidden: [[engineering/secret-plans|The Plans]]

Absent: [[public/nowhere|Nothing Here]]
"""


def test_links_are_resolved_against_what_this_reader_may_see(cli, edit):
    edit("engineering/rendered.md", LINKS)
    r = cli("render", "--draft", "engineering/rendered.md")
    assert 'href="/public/welcome"' in r
    assert "The Plans" in r, "the link text is kept"
    # The path itself must not appear anywhere, live or dead: the disclosure
    # is that the document exists and is called that, and it happens in the
    # HTML long before anybody clicks.
    assert "secret-plans" not in r


def test_forbidden_and_absent_are_indistinguishable(cli, edit):
    edit("engineering/rendered.md", LINKS)
    out = cli("render", "--draft", "engineering/rendered.md").out
    hidden = out.split("Hidden: ", 1)[1].split("\n", 1)[0]
    absent = out.split("Absent: ", 1)[1].split("\n", 1)[0]
    assert hidden == "The Plans</p>"
    assert absent == "Nothing Here</p>"


def test_raw_is_what_a_shared_cache_would_hold(cli, edit):
    """The split the cache depends on: a body is shared, liveness is per-reader."""
    edit("engineering/rendered.md", LINKS)
    r = cli("render", "--raw", "--draft", "engineering/rendered.md")
    assert "/-/fswiki/root.public.welcome" in r
    assert 'href="/public/welcome"' not in r


@pytest.mark.parametrize("hostile,forbidden", [
    ("<script>alert(1)</script>\n", "<script"),
    ("<div onclick=\"steal()\">click</div>\n", "<div"),
    ("[x](javascript:alert(1))\n", 'href="javascript'),
])
def test_a_document_is_written_by_one_user_and_read_by_another(cli, edit, hostile, forbidden):
    edit("engineering/rendered.md", hostile)
    assert forbidden not in cli("render", "--draft", "engineering/rendered.md")
