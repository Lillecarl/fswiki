"""`fswiki-mount --as` and `--as-group`: someone else's view, as a filesystem.

An ACL is a prediction about what a person will be able to see, and the only
honest way to check a prediction is to look. `ls` is the check — which is the
whole argument for putting impersonation behind the mount rather than behind a
report that says which rules matched.

The property that has to hold, and that every test here is a way of asking: an
impersonated mount is a **window**. Nothing that happens inside it can change
anything, and nothing it shows can be mistaken for your own wiki.

See docs/impersonation.md.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import _require, wait_for

pytestmark = pytest.mark.mount


@pytest.fixture(scope="session")
def grants(stack):
    """dave may act as any person, and may compose {everyone, engineering}.

    Session-scoped, unlike the `granted` fixture the HTTP tests use: a mount
    outlives a test, so a grant that vanished between tests would take the
    mount's own credentials with it.
    """
    stack.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'), p.id
            from wiki.principal p where p.name in ('everyone', 'engineering')
        on conflict do nothing;
    """)
    return stack


@pytest.fixture(scope="session")
def as_bob(grants, mount_factory):
    """dave's mount of bob's view."""
    return mount_factory("--as", "bob", user="dave")


@pytest.fixture(scope="session")
def as_engineering(grants, mount_factory):
    """dave's mount of "somebody in everyone and engineering, and nothing else".

    Both groups, not just engineering: a real member of engineering is also a
    member of everyone, and a window that leaves out the second one shows a
    person who does not exist. See docs/impersonation.md.
    """
    return mount_factory("--as-group", "everyone", "--as-group", "engineering",
                         user="dave")


def listing(mount) -> set[str]:
    """Every file in the tree, as display paths."""
    found = set()
    for base, _dirs, files in os.walk(mount.path):
        rel = os.path.relpath(base, mount.path)
        for f in files:
            found.add(f if rel == "." else f"{rel}/{f}")
    return found


# ---------------------------------------------------------------------------
# What the window shows
# ---------------------------------------------------------------------------

def test_a_persons_mount_is_that_persons_tree(as_bob, grants, mount):
    """Not "similar to": the same set, file for file.

    Compared against bob's own mount rather than against a list written here,
    because a hand-written list is a second copy of the ACL and would drift
    from the first one silently.
    """
    assert listing(as_bob) == listing(mount)


def test_what_the_subject_cannot_see_is_not_there(as_bob):
    """bob may read secret-plans and may not sync it, so it is absent from his
    own mount — and therefore absent from a window onto him. An impersonated
    mount that showed *more* than the subject would be the failure that makes
    the whole feature untrustworthy."""
    assert not (as_bob / "engineering/secret-plans.md").exists()


def test_a_group_membership_is_a_person_who_does_not_exist(as_engineering, grants):
    """The synthetic principal is a member of the named groups and nothing
    else, so it reads the union of what those memberships grant — and no
    direct grant to any individual."""
    files = listing(as_engineering)
    assert "public/welcome.md" in files
    assert any(f.startswith("engineering/") for f in files)


def test_the_actors_own_view_is_not_what_is_shown(as_bob, mount_factory, grants):
    """dave is not bob. If the two trees agreed the test would be measuring
    nothing, so this is the guard on every other test in the file."""
    own = mount_factory(user="dave")
    assert listing(own) != listing(as_bob)


# ---------------------------------------------------------------------------
# It is a window, and the kernel enforces that
# ---------------------------------------------------------------------------

def test_the_mount_is_read_only_to_the_kernel(as_bob):
    """`ro` in the mount options, so the write is refused by the kernel before
    it ever reaches us. Three independent layers say read-only here — this one,
    the mode bits below, and the server's read-only transaction — because the
    interesting failures are the ones where a layer is missing."""
    with pytest.raises(OSError) as exc:
        (as_bob / "public/welcome.md").write_text("dave was here")
    assert exc.value.errno == 30  # EROFS


def test_nothing_in_it_is_writable_by_its_mode(as_bob):
    """Belt and braces, and the one a person actually sees: `ls -l` says 0444,
    so an editor refuses to open it for writing rather than discovering EROFS
    on save with the buffer already gone."""
    import stat
    mode = stat.S_IMODE(os.stat(as_bob / "public/welcome.md").st_mode)
    assert not mode & 0o222, oct(mode)


def test_no_drafts_appear_in_a_window(as_bob, clean, mount):
    """The subject's *published* view, not their working copy.

    A draft is unpublished work in progress; showing someone else's would make
    impersonation a way to read over a colleague's shoulder, which is a
    different feature and not one anybody asked for.
    """
    (mount / "engineering/onboarding.md").write_text("bob is mid-thought\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="bob's draft")

    def unchanged():
        return "mid-thought" not in (as_bob / "engineering/onboarding.md").read_text()

    assert wait_for(unchanged, what="the window to keep showing published text")


def test_a_write_that_gets_past_the_kernel_is_still_refused(as_bob, grants, rest):
    """The kernel is the client's own doing, so it is not the guarantee.

    This is the same request the mount would have made, made directly: the
    transaction is read-only on the server, and that is the part no client can
    talk its way out of.
    """
    r = rest("/draft", method="POST", user="dave",
             headers={"Fswiki-Act-As": "bob"},
             body={"author_id": grants.who("bob"), "operation": "update",
                   "path": "root.engineering.onboarding", "content": "no"})
    assert r.code >= 400
    assert "read-only" in r.body or "25006" == r.error.get("code")


# ---------------------------------------------------------------------------
# Saying so
# ---------------------------------------------------------------------------

def test_starting_one_says_whose_view_it_is(as_bob):
    """A tree that looks like your own wiki minus a few pages is
    indistinguishable from your own wiki having lost a few pages. There is no
    banner in a filesystem, so it is said once, at the only moment anyone is
    looking at the terminal — and after everything that could refuse, so it is
    never printed about a mount that did not happen."""
    said = as_bob.log.read_text()
    assert "mounting the view of bob" in said
    assert "not yours" in said and "record" in said


def test_a_group_window_names_the_groups(as_engineering):
    assert "member of everyone, engineering" in as_engineering.log.read_text()


def test_the_system_itself_calls_it_read_only(as_bob):
    """`ro` reaches /proc/mounts, so `mount` and `findmnt` say it too. That is
    the difference between a filesystem that refuses writes and one the kernel
    knows will."""
    line = next(l for l in open("/proc/self/mountinfo")
                if f" {as_bob.path} " in l)
    assert " ro," in line or line.split(" ")[5].startswith("ro")


# ---------------------------------------------------------------------------
# Who is on the record
# ---------------------------------------------------------------------------

def test_the_actor_is_logged_and_the_subject_is_not_accused(as_bob, grants):
    """Reading through the window is dave reading, not bob reading. The
    impersonation log names the actor; the subject's access trail must stay
    empty, or an audit becomes a record of things people did not do."""
    listing(as_bob)  # provoke at least one request
    wait_for(lambda: grants.count(
        "select count(*) from wiki.impersonation_event e "
        "join wiki.principal p on p.id = e.actor_id where p.name = 'dave'") > 0,
        what="the impersonation to be recorded")
    assert grants.count(
        "select count(*) from wiki.access_event e "
        "join wiki.principal p on p.id = e.principal_id where p.name = 'bob'"
    ) == 0


def test_a_session_is_one_row_however_many_requests(as_bob, grants):
    """A poll every second would otherwise make the log unreadable within the
    hour, and an unreadable log is one nobody checks."""
    before = grants.count("select count(*) from wiki.impersonation_event")
    for _ in range(3):
        listing(as_bob)
    after = grants.count("select count(*) from wiki.impersonation_event")
    assert after == before, "a live session grew a second row"


# ---------------------------------------------------------------------------
# Refusals at the door
# ---------------------------------------------------------------------------

def _mount_fails(stack, tmp_path, *flags, user="dave"):
    _require("fswiki-mount")
    base = tmp_path / "mnt"
    base.mkdir()
    out = subprocess.run(["fswiki-mount", str(base), *flags],
                         capture_output=True, text=True, timeout=60,
                         env=stack.env(user))
    assert out.returncode != 0, out.stdout + out.stderr
    return out.stdout + out.stderr


def test_a_person_and_a_membership_at_once_is_refused(stack, tmp_path):
    """They are two different questions — "what does bob see" and "what does a
    member of these groups see" — and answering both at once would mean
    inventing a third principal nobody asked about."""
    assert "pick one" in _mount_fails(
        stack, tmp_path, "--as", "bob", "--as-group", "engineering").lower()


def test_auditing_someone_elses_reads_is_refused(stack, tmp_path):
    """`--audit` files access events against the token holder. Under `--as`
    they would be filed against a session that is not doing the reading, which
    is the one thing an audit trail must never contain."""
    assert "audit" in _mount_fails(
        stack, tmp_path, "--as", "bob", "--audit").lower()


def test_an_ungranted_actor_gets_a_reason_and_not_a_traceback(stack, tmp_path):
    """frank was never granted anything. The refusal comes from the server, and
    the point of the test is that it arrives as a sentence."""
    output = _mount_fails(stack, tmp_path, "--as", "bob", user="frank")
    assert "Traceback" not in output
    assert "not permitted" in output.lower() or "impersonat" in output.lower()
