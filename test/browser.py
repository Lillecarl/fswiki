#!/usr/bin/env python3
"""A browser, logging in through dex, against the deployed wiki.

The same machine `integration.py` drives. This file gets everything up, points
bob's account at dex, and hands over: the browser work is `browser_login.py`,
which runs on the host against ports passt forwards out of the guest.
"""

import asyncio
import json
import os

from uml_runner import run_test

READY = 120
# Firefox's first launch is not fast, and the login is four navigations after
# that.
BROWSE = 600

WIKI = "http://127.0.0.1:8080"
# The address dex calls itself, reachable from inside the guest and from the
# host alike. See the forward in test/default.nix.
DEX = "http://127.0.0.2:5556/dex"


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

    # dex's subject is opaque and is not a username, so each account has to be
    # pointed at it before a token of dex's can resolve to anybody -- and that
    # has to happen before the browser reads a page, which is the last thing
    # the browser does.
    #
    # The subjects come from password grants, which are cheap and already
    # proven in integration.py. Deriving them instead would mean writing dex's
    # encoding of (userID, connector) into this file as four base64 constants,
    # and a constant nobody can read is a constant nobody will fix.
    issuer = (await vm.succeed(
        f"curl -sS {DEX}/.well-known/openid-configuration | jq -r .issuer")).strip()

    for user in settings["identities"]:
        access = (await vm.succeed(
            f"curl -sS -X POST {DEX}/token"
            " -d grant_type=password -d client_id=fswiki"
            " -d client_secret=fswiki-test-secret"
            f" -d username={user}@test.invalid -d password=hunter2"
            " -d scope='openid groups' | jq -r .access_token")).strip()
        assert access and access != "null", f"dex would not issue for {user}"
        subject = (await vm.succeed(
            f"curl -sS -H 'Authorization: Bearer {access}'"
            f" {DEX}/userinfo | jq -r .sub")).strip()
        assert subject and subject != "null", f"dex named no subject for {user}"
        await vm.succeed(
            "psql -U postgres -qAt -d fswiki -v ON_ERROR_STOP=1 -c "
            f"\"update wiki.user_account set oidc_issuer = '{issuer}', "
            f"oidc_subject = '{subject}' "
            "where principal_id = (select id from wiki.principal "
            f"where kind = 'user' and name = '{user}')\""
        )
    print(f"[test] {len(settings['identities'])} accounts now name dex")

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

    # $out is a directory here rather than the empty file mkTest would leave.
    # The screenshots are the deliverable: `nix build --file . tests.browser`
    # and the pictures are under ./result/screens.
    destination = os.environ["out"]
    os.makedirs(destination, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        f"{settings['python']}/bin/python3", settings["script"],
        f"http://{wiki[0]}", f"http://{dex[0]}/dex",
        destination, settings["pngquant"],
        json.dumps(settings["identities"]),
        env={**os.environ,
             "HOME": os.environ.get("TMPDIR", "/tmp"),
             "PLAYWRIGHT_BROWSERS_PATH": settings["browsers"]},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=BROWSE)
    print(out.decode())
    assert proc.returncode == 0, f"the browser script exited {proc.returncode}"

    shots = sum(len(files) for _, _, files in os.walk(destination))
    print(f"[test] {shots} files under $out; the screenshots are the point")


run_test(test)
