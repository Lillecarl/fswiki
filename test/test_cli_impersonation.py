"""`fswiki --as` and `--as-group`: someone else's view, from the terminal.

The mount answers "what can they see" by being a tree you can `ls`. The CLI
answers the narrower questions that do not need a filesystem — what does this
page look like to them, what have they got outstanding — and it is where a
person checks an ACL change without mounting anything.

The property is the same one the mount has, arrived at from a different
direction: **nothing done through a borrowed identity may write**. On the mount
that is enforced three times over; here there is no kernel and no mode bit, so
it rests entirely on the server's read-only transaction. That makes it exactly
the thing to test.

See docs/impersonation.md.
"""

from __future__ import annotations

import pytest

from conftest import wait_for


@pytest.fixture
def granted(clean):
    """dave may act as any person, and may compose {everyone, engineering}."""
    clean.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'), p.id
            from wiki.principal p where p.name in ('everyone', 'engineering')
        on conflict do nothing;
    """)
    return clean


# ---------------------------------------------------------------------------
# Naming the borrowed identity
# ---------------------------------------------------------------------------

def test_whoami_answers_as_the_borrowed_identity(cli, granted):
    """`current_user_id()` is `stable`, so it is read-only, so impersonation
    refuses it; `acting_as()` is the volatile form of the same question. A tool
    that could borrow an identity but not name it would be a poor one for the
    only job impersonation has."""
    r = cli("--as", "bob", "whoami", user="dave")
    assert r.code == 0
    assert granted.who("bob") in r


def test_a_membership_resolves_to_somebody_who_is_not_anybody(cli, granted):
    """The synthetic principal has an id of its own — derived from the sorted
    group set, with the uuid version nibble forced to a value gen_random_uuid()
    can never emit. It is nobody's row, and that is the point."""
    r = cli("--as-group", "everyone", "--as-group", "engineering",
            "whoami", user="dave")
    assert r.code == 0
    real = granted.psql("select array_agg(id::text) from wiki.principal")
    assert r.out.strip() not in real


# ---------------------------------------------------------------------------
# Reading as them
# ---------------------------------------------------------------------------

def test_render_shows_what_the_subject_would_get(cli, granted):
    r = cli("--as", "bob", "render", "public/welcome.md", user="dave")
    assert r.code == 0
    assert "<h1" in r


def test_what_the_subject_cannot_read_cannot_be_rendered(cli, granted):
    """dave is an auditor and can read more than bob can. Borrowing bob has to
    mean borrowing bob's limits too, or the answer is worse than useless: it
    would say a page is visible to someone it is not."""
    forbidden = cli("--as", "bob", "render", "engineering/secret-plans.md",
                    user="dave")
    assert forbidden.code == 1

    # And it says the same thing as a page that is simply not there. Telling
    # the two apart would leak the existence, the location and the title of a
    # document the ACL granted none of — the same rule the renderer applies to
    # links, arrived at from the other end.
    absent = cli("--as", "bob", "render", "engineering/no-such-thing.md",
                 user="dave")
    assert absent.code == 1
    assert (forbidden.out.replace("secret-plans", "X")
            == absent.out.replace("no-such-thing", "X"))


@pytest.mark.mount
def test_status_shows_the_subjects_outstanding_work(cli, granted, mount, clean):
    """A draft is part of what a person sees when they look at their own wiki,
    and "I can't see X" is quite often about a file they have not pushed."""
    (mount / "engineering/onboarding.md").write_text("bob is mid-thought\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="bob's draft")

    r = cli("--as", "bob", "status", user="dave")
    assert r.code == 0
    assert "engineering/onboarding" in r


# ---------------------------------------------------------------------------
# Writing as them: no
# ---------------------------------------------------------------------------

@pytest.mark.mount
def test_a_borrowed_push_is_refused(cli, granted, mount, clean):
    """`document_version.author_id` is permanent published history. An
    impersonated push would forge into it irrecoverably, which is why the
    refusal is `set transaction read only` rather than a list of write paths
    somebody has to keep complete."""
    (mount / "engineering/onboarding.md").write_text("bob is mid-thought\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="bob's draft")
    tip = clean.tip("root.engineering.onboarding")

    r = cli("--as", "bob", "push", "-m", "not mine to publish", user="dave")
    assert r.code == 1
    assert "Traceback" not in r
    assert clean.tip("root.engineering.onboarding") == tip, "the tip moved"
    assert clean.count("select count(*) from wiki.draft") == 1, "the draft moved"


@pytest.mark.mount
def test_a_borrowed_revert_cannot_throw_away_their_work(cli, granted, mount, clean):
    """The destructive direction, and the one that matters more: a window that
    could delete what it is looking at is not a window."""
    (mount / "engineering/onboarding.md").write_text("bob is mid-thought\n")
    wait_for(lambda: clean.count("select count(*) from wiki.draft") == 1,
             what="bob's draft")

    r = cli("--as", "bob", "revert", "--apply", user="dave")
    assert r.code == 1
    assert clean.count("select count(*) from wiki.draft") == 1, "the draft was withdrawn"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_a_person_and_a_membership_at_once_is_refused(cli, granted):
    r = cli("--as", "bob", "--as-group", "engineering", "whoami", user="dave")
    assert r.code == 1
    assert "pick one" in r


def test_an_ungranted_actor_is_told_so(cli, granted):
    """frank was never granted anything. The refusal comes from the server and
    has to arrive as a sentence — a 42501 with a stack trace under it would
    send someone to read the client's source about a decision the client did
    not make."""
    r = cli("--as", "bob", "whoami", user="frank")
    assert r.code == 1
    assert "Traceback" not in r


def test_no_stepping_up_to_a_superuser(cli, granted):
    """dave's grant names `everyone`, which expands to cover alice, and alice
    is a superuser. The refusal must not say why: that would answer a question
    the caller was not entitled to ask."""
    r = cli("--as", "alice", "whoami", user="dave")
    assert r.code == 1
    assert "superuser" not in r


def test_borrowing_leaves_the_actor_on_the_record(cli, granted):
    """The human, never the subject. An audit trail that can be written as
    someone else is worse than none, because it is trusted."""
    cli("--as", "bob", "whoami", user="dave")
    assert granted.count(
        "select count(*) from wiki.impersonation_event e "
        "join wiki.principal p on p.id = e.actor_id where p.name = 'dave'") == 1
    assert granted.count(
        "select count(*) from wiki.impersonation_event e "
        "join wiki.principal p on p.id = e.subject_id where p.name = 'dave'") == 0
