"""Mint a dev JWT for a fixture user.

PostgREST verifies the signature and copies the whole payload into the
`request.jwt.claims` GUC; `wiki.current_user_id()` then resolves the principal
from (iss, sub). The `role` claim is what PostgREST does SET ROLE with, so it
has to name a real database role.

    fswiki-token bob                     # a token for bob
    fswiki-token --role fswiki_anon bob  # what an unauthenticated caller gets

Dev only. The secret lives in plain text under the state directory.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import jwt

FIXTURE_USERS = ["alice", "bob", "carol", "dave", "erin", "frank", "grace"]


def main() -> int:
    ap = argparse.ArgumentParser(prog="fswiki-token", description=__doc__)
    ap.add_argument("subject", help="oidc_subject, i.e. the username")
    ap.add_argument("--role", default="fswiki_user", help="database role to assume")
    ap.add_argument(
        "--issuer",
        default=os.environ.get("FSWIKI_ISSUER", "https://idp.test"),
        help="must match user_account.oidc_issuer",
    )
    ap.add_argument("--ttl", type=int, default=86400, help="seconds until expiry")
    ap.add_argument(
        "--secret-file",
        type=Path,
        default=None,
        help="defaults to $FSWIKI_STATE/jwt-secret",
    )
    ap.add_argument(
        "--header",
        action="store_true",
        help="print a full Authorization header instead of the bare token",
    )
    args = ap.parse_args()

    secret_file = args.secret_file
    if secret_file is None:
        state = os.environ.get("FSWIKI_STATE")
        if not state:
            print(
                "fswiki-token: set FSWIKI_STATE or pass --secret-file\n"
                "              (eval \"$(fswiki-dev env)\")",
                file=sys.stderr,
            )
            return 2
        secret_file = Path(state) / "jwt-secret"

    if not secret_file.is_file():
        print(f"fswiki-token: no secret at {secret_file}; is the stack up?", file=sys.stderr)
        return 2

    secret = secret_file.read_text().strip()

    if args.subject not in FIXTURE_USERS:
        print(
            f"fswiki-token: note, {args.subject!r} is not a fixture user "
            f"({', '.join(FIXTURE_USERS)}); the token will verify but resolve to no principal",
            file=sys.stderr,
        )

    now = int(time.time())
    token = jwt.encode(
        {
            "role": args.role,
            "iss": args.issuer,
            "sub": args.subject,
            "iat": now,
            "exp": now + args.ttl,
        },
        secret,
        algorithm="HS256",
    )

    print(f"Authorization: Bearer {token}" if args.header else token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
