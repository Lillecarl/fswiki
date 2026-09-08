#!/usr/bin/env python3
"""A browser, logging in through dex, against the deployed wiki.

The same machine `integration.py` drives. This file gets everything up, points
bob's account at dex, and hands over: the browser work is `browser_login.py`,
which runs on the host against ports passt forwards out of the guest.
"""

import asyncio
import os

from uml_runner import run_test

READY = 120
# Firefox's first launch is not fast, and the login is four navigations after
# that.
BROWSE = 600

WIKI = "http://127.0.0.1:8080"


async def test(vms):
    vm, settings = vms.node, vms.settings

    await vm.wait_for_unit("postgresql.service")
    await vm.wait_for_unit("dex.service")
    await vm.wait_for_unit("fswiki.service")

    try:
        await vm.succeed(
            f"until curl -sf --max-time 2 {WIKI}/ >/dev/null; do sleep 1; done",
            timeout=READY,
        )
    except Exception:
        print("[test] fswiki did not bind; its journal follows")
        print(await vm.journal("fswiki"))
        raise
    print("[test] the wiki answers")

    # Somebody to be. See integration.py: a deployed wiki is empty.
    for name in ("fixtures", "seed"):
        await vm.succeed(
            f"psql -U postgres -qAt -d fswiki -v ON_ERROR_STOP=1 -f {settings[name]}",
            timeout=120,
        )

    # dex's subject is opaque and not a username, so bob's account has to be
    # pointed at it before a token of dex's can resolve to him -- and that has
    # to happen before the browser reads a page, which is the last thing the
    # browser does.
    #
    # So the subject is fetched here with a password grant, which is cheap and
    # already proven in integration.py. Deriving it instead would mean writing
    # dex's encoding of (userID, connector) into this file as a base64 constant,
    # and a constant nobody can read is a constant nobody will fix.
    issuer = (await vm.succeed(
        "curl -sS http://127.0.0.1:5556/dex/.well-known/openid-configuration"
        " | jq -r .issuer")).strip()
    access = (await vm.succeed(
        "curl -sS -X POST http://127.0.0.1:5556/dex/token"
        " -d grant_type=password -d client_id=fswiki"
        " -d client_secret=fswiki-test-secret"
        " -d username=bob@test.invalid -d password=hunter2"
        " -d scope='openid groups' | jq -r .access_token")).strip()
    subject = (await vm.succeed(
        f"curl -sS -H 'Authorization: Bearer {access}'"
        " http://127.0.0.1:5556/dex/userinfo | jq -r .sub")).strip()
    assert subject and subject != "null", "dex named no subject"

    await vm.succeed(
        "psql -U postgres -qAt -d fswiki -v ON_ERROR_STOP=1 -c "
        f"\"update wiki.user_account set oidc_issuer = '{issuer}', "
        f"oidc_subject = '{subject}' "
        "where principal_id = (select id from wiki.principal "
        "where kind = 'user' and name = 'bob')\""
    )
    print("[test] bob's account names dex")

    # --- and now the browser, on this side ---------------------------------
    #
    # The browser runs here rather than in the guest, which is where a person's
    # browser is anyway. It has to: io_uring does not work under User-Mode
    # Linux, every browser reaches for it, and a guest that is asked to run one
    # prints "BUG: Bad page map ... file:[io_uring]" and stops answering.
    #
    # passt is the way in. The address was fixed in test/default.nix rather than
    # handed out, because dex compares a redirect_uri exactly and had to be told
    # it before anything booted.
    wiki = vm.reachable(8080)
    dex = vm.reachable(5556)
    assert wiki and dex, f"not forwarded: wiki={wiki} dex={dex}"
    print(f"[test] the host reaches the wiki on {wiki[0]} and dex on {dex[0]}")

    proc = await asyncio.create_subprocess_exec(
        f"{settings['python']}/bin/python3", settings["script"],
        f"http://{wiki[0]}", f"http://{dex[0]}/dex",
        env={**os.environ,
             "HOME": os.environ.get("TMPDIR", "/tmp"),
             "PLAYWRIGHT_BROWSERS_PATH": settings["browsers"]},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=BROWSE)
    print(out.decode())
    assert proc.returncode == 0, f"the browser script exited {proc.returncode}"
    print("[test] logged in through a browser")


run_test(test)
