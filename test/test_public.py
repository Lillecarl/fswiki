"""What an unauthenticated HTTP request can reach.

`server/test/070_public_test.sql` covers the same ground in the database, by
dropping to fswiki_anon with SET ROLE. That tests the grants; it does not test
PostgREST's half of the arrangement — that a request with no Authorization
header switches to db-anon-role at all, that the RPCs are exposed to it, and
that a refusal comes back as a status rather than as content. The two halves
have to agree, and a wiki holding company secrets is the wrong place to assume
they do.

Everything here is a request with no token.
"""

from __future__ import annotations

import json

import pytest

PUBLIC = "root.notices"


@pytest.fixture
def published(stack):
    """One page granted to `public`, removed again afterwards.

    Not in `010_fixtures.sql`, deliberately: a document readable by everyone
    would be visible to erin, and `020_rls_test.sql` asserts that erin sees
    literally nothing. That assertion is worth more than the convenience.
    """
    stack.exec(f"""
        insert into wiki.document (parent_id, slug, is_folder, title, owner_id)
        select d.id, 'notices', false, 'Notices',
               (select p.id from wiki.principal p
                 where p.kind = 'user' and p.name = 'alice')
          from wiki.document d where d.path = 'root'::ltree;

        insert into wiki.document_version
               (document_id, version, path, content, message, author_id)
        select d.id, 1, d.path, 'Public notice.', 'initial',
               (select p.id from wiki.principal p
                 where p.kind = 'user' and p.name = 'alice')
          from wiki.document d where d.path = '{PUBLIC}'::ltree;

        insert into wiki.ace (document_id, principal_id, role_id, ace_type)
        select d.id,
               (select p.id from wiki.principal p
                 where p.kind = 'group' and p.name = 'public'),
               (select r.id from wiki.role r where r.name = 'reader'),
               'allow'
          from wiki.document d where d.path = '{PUBLIC}'::ltree;
    """)
    yield stack.scalar(f"select id from wiki.document where path = '{PUBLIC}'::ltree")
    stack.exec(f"""
        delete from wiki.ace a using wiki.document d
         where a.document_id = d.id and d.path = '{PUBLIC}'::ltree;
        delete from wiki.document_version v using wiki.document d
         where v.document_id = d.id and d.path = '{PUBLIC}'::ltree;
        delete from wiki.document where path = '{PUBLIC}'::ltree;
    """)


# --- what it can see -------------------------------------------------------

def test_a_request_with_no_token_reaches_the_public_page(published, rest):
    r = rest("/document?select=path&order=path", user=None)
    assert r.code == 200, r.body
    assert [row["path"] for row in json.loads(r.body)] == ["root", PUBLIC]


def test_and_nothing_else_in_the_tree(published, rest):
    r = rest("/document?select=path", user=None)
    paths = {row["path"] for row in json.loads(r.body)}
    assert not any(p.startswith("root.engineering") for p in paths)


def test_the_browser_read_serves_it(published, rest):
    r = rest("/rpc/view_document", user=None, method="POST",
             body={"p_document": published})
    assert r.code == 200, r.body
    assert json.loads(r.body) == [{"content": "Public notice."}]


def test_a_page_it_may_not_read_is_a_page_that_is_not_there(stack, rest):
    """The property the whole project turns on, at the one identity where it is
    easiest to get wrong. Both answers must be the empty list — not a 403 for
    one and an empty list for the other, which would be the disclosure itself."""
    secret = stack.doc("root.engineering.secret-plans")
    missing = "00000000-0000-0000-0000-000000000000"
    refused = rest("/rpc/view_document", user=None, method="POST",
                   body={"p_document": secret})
    absent = rest("/rpc/view_document", user=None, method="POST",
                  body={"p_document": missing})
    assert refused.code == absent.code
    assert refused.body == absent.body == "[]"


# --- what it cannot ---------------------------------------------------------

@pytest.mark.parametrize("table", [
    "principal", "user_account", "group_member", "ace", "draft",
    "role", "role_capability", "access_event",
])
def test_the_user_directory_and_the_acl_stay_shut(rest, table):
    r = rest(f"/{table}?select=*", user=None)
    assert r.code >= 400, f"{table} answered {r.code}: {r.body}"


def test_it_cannot_ask_what_somebody_else_may_read(stack, rest):
    """The oracle the lockdown exists to prevent. The long form of the ACL walk
    takes the principal to judge as an argument, so reaching it without an
    account would mean asking about a stranger, as the owner, with RLS out of
    the picture."""
    r = rest("/rpc/has_capability", user=None, method="POST",
             body={"p_document": stack.doc("root.engineering.secret-plans"),
                   "p_cap": "read", "p_user": stack.who("bob")})
    assert r.code >= 400, r.body


def test_it_cannot_use_the_mirroring_read(published, rest):
    """read_document is the sync client's audited read. Anonymous callers get
    view_document and nothing else."""
    r = rest("/rpc/read_document", user=None, method="POST",
             body={"p_document": published})
    assert r.code >= 400, r.body


def test_it_cannot_resolve_identity(rest):
    r = rest("/rpc/acting_as", user=None, method="POST", body={})
    assert r.code >= 400, r.body


def test_it_cannot_create_a_document(stack, rest):
    r = rest("/document", user=None, method="POST",
             body={"parent_id": stack.doc("root"), "slug": "intruder",
                   "is_folder": False, "title": "Intruder"})
    assert r.code >= 400, r.body


def test_it_cannot_edit_the_public_page(published, rest):
    r = rest(f"/document?id=eq.{published}", user=None, method="PATCH",
             body={"title": "Owned"})
    assert r.code >= 400, r.body


def test_it_cannot_publish(rest):
    r = rest("/rpc/push", user=None, method="POST", body={"p_message": "hi"})
    assert r.code >= 400, r.body


# --- and cannot become somebody ---------------------------------------------

# Over POST, and that matters. Impersonation refuses any read-only transaction
# so that it can always write its own log, and PostgREST runs every GET
# read-only — so an anonymous GET carrying these headers is refused for a
# reason that has nothing to do with having no account, and would pass this
# file whether or not the grant check worked. The POST below is a request
# anonymous callers are otherwise allowed to make, and
# test_the_browser_read_serves_it is the proof that they are: the only
# difference here is the header.

def test_it_cannot_borrow_a_person_by_asking(published, rest):
    """pre_request() has to be executable by fswiki_anon — PostgREST runs
    db-pre-request on every request — and it takes its instructions from
    headers anyone can send. begin_impersonation() checks a grant against the
    authenticated user, and there isn't one."""
    r = rest("/rpc/view_document", user=None, method="POST",
             body={"p_document": published},
             headers={"Fswiki-Act-As": "alice"})
    assert r.code >= 400, r.body


def test_it_cannot_borrow_a_membership_either(published, rest):
    r = rest("/rpc/view_document", user=None, method="POST",
             body={"p_document": published},
             headers={"Fswiki-Act-As-Groups": "engineering"})
    assert r.code >= 400, r.body


def test_a_refused_impersonation_leaves_the_page_alone(published, rest):
    """The refusal must not be a way to see more than before, either."""
    r = rest("/document?select=path&order=path", user=None)
    assert [row["path"] for row in json.loads(r.body)] == ["root", PUBLIC]


# --- the controls -----------------------------------------------------------
#
# Every assertion above says "anonymous gets a 4xx", and a 4xx is also what a
# route that does not exist returns. Without these, a typo in a path would look
# exactly like security. Each one asserts that the same request works for
# somebody with an account, so the refusal is about the caller and not about
# the URL.

@pytest.mark.parametrize("table", [
    "principal", "user_account", "group_member", "ace", "draft",
    "role", "role_capability", "access_event",
])
def test_control_a_named_user_reaches_those_tables(rest, table):
    r = rest(f"/{table}?select=*&limit=1")
    assert r.code == 200, f"{table} answered {r.code}: {r.body}"


def test_control_a_named_user_may_ask_about_another(stack, rest):
    r = rest("/rpc/has_capability", method="POST",
             body={"p_document": stack.doc("root.engineering.secret-plans"),
                   "p_cap": "read", "p_user": stack.who("bob")})
    assert r.code == 200, r.body


def test_control_a_named_user_may_use_the_mirroring_read(stack, rest):
    r = rest("/rpc/read_document", method="POST",
             body={"p_document": stack.doc("root.engineering.onboarding")})
    assert r.code == 200, r.body


def test_control_acting_as_is_a_real_endpoint(rest):
    r = rest("/rpc/acting_as", method="POST", body={})
    assert r.code == 200, r.body


def test_control_the_act_as_headers_work_for_somebody_granted_them(stack, clean, rest):
    """The impersonation refusals above must be about having no account, not
    about the header being ignored or the grant being missing for everyone."""
    clean.exec("""
        insert into wiki.impersonation_grant (actor_id, subject_id)
          select (select id from wiki.principal where name = 'dave'),
                 (select id from wiki.principal where name = 'everyone')
        on conflict do nothing;
    """)
    r = rest("/rpc/view_document", user="dave", method="POST",
             body={"p_document": stack.doc("root.public.welcome")},
             headers={"Fswiki-Act-As-Groups": "everyone"})
    assert r.code == 200, r.body
