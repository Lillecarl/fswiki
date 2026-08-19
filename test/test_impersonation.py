"""Impersonation over real PostgREST.

The SQL suite covers the rules; what it cannot reach is the part that decides
the whole shape, because PostgREST picks the transaction's mode before any SQL
of ours runs. Everything about the hook therefore has to be tested from the
outside.

See docs/impersonation.md.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def granted(clean):
    """dave may act as any person, and may compose {everyone, engineering}.

    A grant naming `everyone` covers every *person*, because people are in it.
    It does not cover a group, because groups are in nothing — so engineering
    is named separately, which is the asymmetry the design is deliberate about.
    """
    stack = clean
    stack.exec("""
        insert into wiki.principal (kind, name)
          select 'group', 'marketing'
           where not exists (select 1 from wiki.principal where name = 'marketing');
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'), p.id
            from wiki.principal p where p.name in ('everyone', 'engineering')
        on conflict do nothing;
    """)
    return stack


def read(rest, doc, **kw):
    return rest("/rpc/read_document", method="POST", body={"p_document": doc}, **kw)


def acting(who=None, groups=None) -> dict:
    if who:
        return {"Fswiki-Act-As": who}
    return {"Fswiki-Act-As-Groups": ",".join(groups)}


# ---------------------------------------------------------------------------
# The ordinary case is untouched
# ---------------------------------------------------------------------------

def test_a_request_without_the_header_is_an_ordinary_request(granted, rest):
    r = read(rest, granted.doc("root.engineering.onboarding"), user="bob")
    assert r.code == 200 and "content" in r
    assert granted.count("select count(*) from wiki.impersonation_event") == 0


# ---------------------------------------------------------------------------
# Acting as a person
# ---------------------------------------------------------------------------

def test_acting_as_a_person_reads_what_they_read(granted, rest):
    doc = granted.doc("root.engineering.onboarding")
    # Without the header the same request must come back empty, or the header
    # is not what changed the answer and this proves nothing.
    assert read(rest, doc, user="dave").body == "[]"
    r = read(rest, doc, user="dave", headers=acting("bob"))
    assert r.code == 200 and "content" in r


def test_the_impersonation_is_on_the_record(granted, rest):
    read(rest, granted.doc("root.engineering.onboarding"),
         user="dave", headers=acting("bob"))
    assert granted.count("""
        select count(*) from wiki.impersonation_event e
          join wiki.principal a on a.id = e.actor_id
          join wiki.principal s on s.id = e.subject_id
         where a.name = 'dave' and s.name = 'bob'""") == 1


def test_the_record_names_the_actor_never_the_subject(granted, rest):
    read(rest, granted.doc("root.engineering.onboarding"),
         user="dave", headers=acting("bob"))
    assert granted.count("""
        select count(*) from wiki.impersonation_event e
          join wiki.principal a on a.id = e.actor_id where a.name = 'bob'""") == 0


# ---------------------------------------------------------------------------
# Acting as a membership
# ---------------------------------------------------------------------------

def test_acting_as_a_membership_reads_what_a_member_reads(granted, rest):
    r = read(rest, granted.doc("root.engineering.onboarding"),
             user="dave", headers=acting(groups=["everyone", "engineering"]))
    assert r.code == 200 and "content" in r


def test_a_membership_is_recorded_as_a_group_set(granted, rest):
    read(rest, granted.doc("root.engineering.onboarding"),
         user="dave", headers=acting(groups=["everyone", "engineering"]))
    assert granted.count(
        "select count(*) from wiki.impersonation_event "
        "where subject_groups is not null and subject_id is null") == 1


def test_a_membership_sees_exactly_what_a_real_member_sees(granted, rest):
    """The measurement the whole design rests on.

    bob is in exactly {everyone, engineering} and has no ACE naming him, so a
    principal whose only memberships are those two must land on his view — not
    a matching count, the same documents.
    """
    mine = {r["path"] for r in rest("/syncable_document?select=path", user="bob").json}
    theirs = {r["path"] for r in rest(
        "/rpc/list_documents?select=path", method="POST", body={}, user="dave",
        headers=acting(groups=["everyone", "engineering"])).json}
    assert mine == theirs
    assert mine, "comparing two empty sets would pass and mean nothing"


def test_naming_the_group_alone_reads_both_too_little_and_too_much(granted, rest):
    """Why the unit is a set of groups and not a group.

    Under-reports, because a group belongs to no other group and nobody is in
    only one. Over-reports, because a deny naming `everyone` never reaches a
    principal that is not in it — so the bare group may mirror a document no
    human in this wiki may mirror.
    """
    granted.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'),
                 (select id from wiki.principal where name = 'engineering')
        on conflict do nothing""")
    bob = {r["path"] for r in rest("/syncable_document?select=path", user="bob").json}
    alone = {r["path"] for r in rest(
        "/rpc/list_documents?select=path", method="POST", body={}, user="dave",
        headers=acting(groups=["engineering"])).json}

    assert bob - alone, "the group alone should be missing what a real member has"
    assert "root.engineering.secret-plans" in alone
    assert "root.engineering.secret-plans" not in bob


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

def test_an_impersonated_write_is_refused_by_the_transaction(granted, rest):
    """Nothing in draft's policies said no. The transaction did."""
    r = rest("/draft", method="POST", user="dave", headers=acting("bob"), body={
        "author_id": granted.who("bob"), "operation": "create",
        "path": "root.public.forged", "content": "not bob",
    })
    assert r.error.get("code") == "25006"
    assert granted.count(
        "select count(*) from wiki.draft where path = 'root.public.forged'") == 0


def test_an_impersonated_push_cannot_write_even_what_the_subject_may(granted, rest):
    """A draft bob genuinely has `write` on, so only the lock can stop it."""
    granted.exec("""
        insert into wiki.draft (author_id, operation, document_id, path, content, base_version)
          select p.id, 'update', d.id, d.path, 'dave was here', d.version
            from wiki.principal p, wiki.current_document d
           where p.name = 'bob' and d.path = 'root.engineering.onboarding'""")
    r = rest("/rpc/push", method="POST", body={"p_message": "x"},
             user="dave", headers=acting("bob"))
    assert r.error.get("code") == "25006"
    assert granted.count(
        "select count(*) from wiki.draft where content = 'dave was here'") == 1


def test_the_pooled_connection_goes_back_writable(granted, rest):
    """`true` on set_config, so the lock is the transaction's and not the pool's."""
    rest("/rpc/read_document", method="POST", user="dave", headers=acting("bob"),
         body={"p_document": granted.doc("root.engineering.onboarding")})
    r = rest("/draft", method="POST", user="bob", body={
        "author_id": granted.who("bob"), "operation": "create",
        "path": "root.public.after-imp", "content": "y",
    })
    assert r.code < 400, r


# ---------------------------------------------------------------------------
# No impersonation without a record of it
# ---------------------------------------------------------------------------

def test_a_get_carrying_the_header_is_refused_with_a_reason(granted, rest):
    r = rest("/syncable_document?select=id", user="dave", headers=acting("bob"))
    assert r.error.get("code") == "42501"
    assert "read-only transaction" in r.error.get("message", "")


def test_the_rule_is_volatility_and_not_the_verb(granted, rest):
    """change_token is `stable`, so PostgREST runs it read-only over POST too.

    A guard on the method would have looked right and failed later as a bare
    25006 on an endpoint that looked like it should work.
    """
    r = rest("/rpc/change_token", method="POST", body={},
             user="dave", headers=acting("bob"))
    assert r.error.get("code") == "42501"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_an_ungranted_actor_is_refused(granted, rest):
    r = read(rest, granted.doc("root.public.welcome"), user="bob", headers=acting("dave"))
    assert r.error.get("code") == "42501"


def test_never_step_up_to_a_superuser(granted, rest):
    """dave's grant expands to cover alice. alice is a superuser. dave is not."""
    r = read(rest, granted.doc("root.public.welcome"), user="dave", headers=acting("alice"))
    assert r.error.get("code") == "42501"
    # And the refusal must not say *why*: that would answer a question the
    # caller was not entitled to ask.
    assert "superuser" not in r.body


def test_a_group_the_actor_was_not_granted_is_refused(granted, rest):
    r = read(rest, granted.doc("root.public.welcome"),
             user="dave", headers=acting(groups=["marketing"]))
    assert r.error.get("code") == "42501"


@pytest.mark.parametrize("headers,why", [
    ({"Fswiki-Act-As-Groups": "bob"}, "a person is not a membership"),
    ({"Fswiki-Act-As": "nobody-at-all"}, "an unknown name raises"),
    ({"Fswiki-Act-As": "bob", "Fswiki-Act-As-Groups": "everyone"},
     "both at once is an error, not a precedence rule"),
])
def test_the_headers_are_refused_rather_than_interpreted(granted, rest, headers, why):
    """Acting as nobody would look indistinguishable from the feature working."""
    r = read(rest, granted.doc("root.public.welcome"), user="dave", headers=headers)
    assert r.error.get("code") == "22023", why


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------

def test_an_impersonated_read_writes_no_access_event(granted, rest):
    """It writes an impersonation_event instead, which is the truer record.

    Filing it a second time as an ordinary read by someone who was not reading
    would be the laundering the acted_as column exists to prevent.
    """
    read(rest, granted.doc("root.engineering.onboarding"),
         user="dave", headers=acting("bob"))
    assert granted.count("select count(*) from wiki.access_event") == 0
    assert granted.count("select count(*) from wiki.impersonation_event") == 1


def test_an_actor_reads_their_own_log_and_the_subject_does_not(granted, rest):
    read(rest, granted.doc("root.engineering.onboarding"),
         user="dave", headers=acting("bob"))
    assert len(rest("/impersonation_event?select=id", user="dave").json) == 1
    assert rest("/impersonation_event?select=id", user="bob").json == []


def test_repeat_requests_extend_one_session(granted, rest):
    """The hook runs per request and a client makes a great many.

    A row each would bury the fact anyone cares about under its own volume.
    """
    doc = granted.doc("root.engineering.onboarding")
    for _ in range(3):
        read(rest, doc, user="dave", headers=acting("bob"))
    assert granted.count("select count(*) from wiki.impersonation_event") == 1
    assert granted.count(
        "select requests from wiki.impersonation_event") == 3


def test_the_rpc_reads_are_the_get_reads(granted, rest):
    """If these ever disagree, one has become a second opinion about what a
    caller may see, rather than the same read over another transport."""
    via_get = [r["path"] for r in rest("/syncable_document?select=path&order=path",
                                       user="bob").json]
    via_rpc = [r["path"] for r in rest("/rpc/list_documents?select=path",
                                       method="POST", body={}, user="bob").json]
    assert via_get == via_rpc
