"""A token this wiki did not sign.

Every other test in the suite mints an HS256 token with a secret it owns, which
is the right shape for a suite: the secret is a fixture and the signature is not
what is under test. But it means the whole suite proves only that a wiki accepts
its own tokens, and a wiki with a shared secret is not a wiki with an identity
provider.

So this asks the other question. An RSA key stands in for an issuer. Its public
half is published as a JWK Set, exactly as an OIDC provider publishes one at
`/.well-known/jwks.json`, and PostgREST is started with that Set as its
`jwt-secret`. Nothing else changes: the token claims the same `iss` and `sub`
as the fixtures already carry, so if the identity plumbing is sound it resolves
to the same principal and reads the same page.

What is genuinely being checked is the seam between three parties that never
meet. The issuer signs. PostgREST verifies, against a key it was handed rather
than a secret it shares. Postgres resolves `(iss, sub)` to a principal in
`wiki.authenticated_user_id()`, and neither of the other two knows that
function exists.

This is also the piece a Keycloak test would sit on. Booting a real provider
answers "does Keycloak issue what we expect"; it cannot tell you whether an
RSA-signed token reaches the right row, because a failure there looks the same
either way. That question is answerable in a second, in a build sandbox, and it
is answered here.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from conftest import ISSUER, _start_postgrest, free_port, http

# 2048 rather than 4096: this is generated per session and the number under test
# is the algorithm, not the modulus. Two of them, because "signed by a key the
# server does not have" is a case, and the only honest way to write it is with a
# key the server really does not have.
KEY_BITS = 2048


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_int(value: int) -> str:
    """A JWK's big-endian, minimal-length, base64url integer."""
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class Issuer:
    """One RSA key, and the two things a relying party ever sees of it.

    `jwks` is what the provider publishes. `sign` is what only the provider can
    do. Keeping both on one object is what makes the negative case readable: a
    second Issuer is a second provider, and its tokens are somebody else's.
    """

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537,
                                             key_size=KEY_BITS)

    @property
    def jwks(self) -> dict:
        numbers = self._key.public_key().public_numbers()
        return {"keys": [{
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
            "n": _b64u_int(numbers.n),
            "e": _b64u_int(numbers.e),
        }]}

    def token(self, subject: str, *, issuer: str = ISSUER,
              role: str | None = "fswiki_user", ttl: int = 3600) -> str:
        """An RS256 token, assembled in the open.

        Written out rather than handed to pyjwt for the same reason
        `conftest.mint_jwt` is: a test that wants to argue about one claim
        should be able to see every claim from where it stands.
        """
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        payload = {"iss": issuer, "sub": subject, "iat": now, "exp": now + ttl}
        if role is not None:
            payload["role"] = role
        signing = (_b64u(json.dumps(header).encode()) + "."
                   + _b64u(json.dumps(payload).encode())).encode()
        sig = self._key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing.decode()}.{_b64u(sig)}"


@pytest.fixture(scope="module")
def idp() -> Issuer:
    """The provider the wiki trusts."""
    return Issuer("fswiki-test-1")


@pytest.fixture(scope="module")
def impostor() -> Issuer:
    """A provider it has never heard of, holding a key it does not publish."""
    return Issuer("fswiki-test-2")


@pytest.fixture(scope="module")
def oidc(stack, idp, tmp_path_factory):
    """A PostgREST that verifies signatures instead of sharing a secret.

    Module-scoped and separate from the session's own server, which keeps its
    HS256 secret so that every other test goes on working. The two speak to the
    same cluster, so a principal is the same principal from either.
    """
    port = free_port()
    log = tmp_path_factory.mktemp("oidc") / "postgrest.log"
    proc = _start_postgrest(
        stack, port, log,
        secret=json.dumps(idp.jwks),
        probe_token=idp.token("bob"),
    )
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _whoami(url: str, token: str | None):
    return http(f"{url}/rpc/authenticated_user_id", token=token)


def test_a_signed_token_resolves_to_the_principal(stack, oidc, idp):
    """The whole point: an RSA signature reaches the same row a shared secret did.

    Asserted against the id from the database rather than against "not null".
    A token that verified but resolved to the wrong person would satisfy the
    weaker check, and that is the failure worth having a test for.
    """
    got = _whoami(oidc, idp.token("bob"))
    assert got.code == 200, got.body
    assert got.json == stack.who("bob")


def test_the_page_bob_could_already_read(stack, oidc, idp):
    """And it is a whole identity, not just a lookup.

    `authenticated_user_id()` answering correctly does not prove the request is
    running as bob -- PostgREST SET ROLEs from the `role` claim, and RLS is
    evaluated against that role. So read something through a policy.
    """
    rows = http(f"{oidc}/current_document?select=path&limit=1",
                token=idp.token("bob"))
    assert rows.code == 200, rows.body
    assert rows.json, "bob reads nothing; the fixtures or the role claim moved"


def test_another_issuers_signature_is_refused(oidc, impostor):
    """A well-formed token, correctly signed, by the wrong key.

    401 and not 403: PostgREST is rejecting the token, which happens before any
    question about a document is asked. Nothing here reaches Postgres at all.
    """
    refused = _whoami(oidc, impostor.token("bob"))
    assert refused.code == 401, refused.body


def test_a_tampered_payload_is_refused(oidc, idp):
    """The signature covers the claims, not just the header.

    Swapping the payload for one naming alice -- who is a superuser in the
    fixtures -- leaves the signature attached to a body it was not made for.
    """
    header, _, sig = idp.token("bob").split(".")
    forged = json.dumps({"role": "fswiki_user", "iss": ISSUER, "sub": "alice",
                         "iat": int(time.time()), "exp": int(time.time()) + 3600})
    refused = _whoami(oidc, f"{header}.{_b64u(forged.encode())}.{sig}")
    assert refused.code == 401, refused.body


def test_the_right_key_from_the_wrong_issuer_is_nobody(oidc, idp):
    """Verification and identity are two questions, and this is the gap.

    The signature is good -- the key is the one the wiki was given -- so
    PostgREST is satisfied and the request runs. But `wiki.user_account` is keyed
    on (oidc_issuer, oidc_subject), so an `iss` nobody registered resolves to no
    principal at all. Not an error: an authenticated request that is nobody, and
    it must see exactly what nobody sees.

    That distinction is what stops a second realm on the same provider from
    silently becoming the first one's users.
    """
    token = idp.token("bob", issuer="https://elsewhere.test")
    got = _whoami(oidc, token)
    assert got.code == 200, got.body
    assert got.json is None

    rows = http(f"{oidc}/current_document?select=path", token=token)
    assert rows.code == 200, rows.body
    assert rows.json == []


def test_an_expired_token_is_refused(oidc, idp):
    """exp is checked, and by PostgREST rather than by us."""
    refused = _whoami(oidc, idp.token("bob", ttl=-60))
    assert refused.code == 401, refused.body


@pytest.mark.parametrize("role", [None, "fswiki_anon"],
                         ids=["no role claim", "role: fswiki_anon"])
def test_the_role_claim_does_not_narrow_what_a_token_holder_reads(oidc, idp, role):
    """A downgraded role is not a downgraded identity, and that surprises people.

    `role` is what PostgREST does SET ROLE with, so dropping it -- or naming
    fswiki_anon outright -- really does change the database role. The evidence is
    a refusal: only fswiki_user holds EXECUTE on authenticated_user_id(), so
    42501 comes back, which is Postgres speaking and therefore proof the request
    ran there as somebody else.

    It does not change what the request can read. `060_roles.sql` says it in one
    line -- table privileges are the coarse gate, RLS is the fine one -- and RLS
    resolves the caller from the `iss` and `sub` claims, which are still bob's.
    So wherever fswiki_anon holds a table grant, the rows that come back are the
    ones bob may see. Measured, not assumed: this is bob's whole tree, not the
    root alone that a request with no token gets.

    This is the design and not a hole -- bob sees bob's rows, never more, and
    `test_the_right_key_from_the_wrong_issuer_is_nobody` is the same rule read
    the other way. It is pinned because it is the assumption an OIDC deployment
    is most likely to get wrong: a mapper that emits the wrong role, or a
    "public" endpoint built by stripping the claim, restricts which tables the
    request may touch and not one row of what it sees. If a request must be
    anonymous, it must carry no identity -- not a smaller role.
    """
    token = idp.token("bob", role=role)

    refused = _whoami(oidc, token)
    assert refused.error.get("code") == "42501", refused.body

    served = {row["path"] for row in http(
        f"{oidc}/document?select=path", token=token).json}
    anonymous = {row["path"] for row in http(
        f"{oidc}/document?select=path", token=None).json}

    assert any(p.startswith("root.engineering") for p in served), served
    assert served > anonymous

