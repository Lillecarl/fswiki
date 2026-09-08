#!/usr/bin/env python3
"""The whole thing, deployed.

Not a test suite against a stack a fixture built. A NixOS machine, running the
units a deployment runs: `postgresql`, `dex` for an identity provider, and
`fswiki` from `nix/module.nix`. Nothing here knows how any of them start.

What that buys over `tests.check` is everything the module decides and no test
in-process can reach. Peer authentication over the socket, with one OS user
arriving as two database users -- and the second one deliberately without DDL,
because an owner is not subject to RLS and a PostgREST connected as the owner
would bypass every policy while looking healthy. A JWKS fetched from a provider
that is still starting. A `role` that is not a claim any provider emits, mapped
out of `groups`. The order of the three, which nothing but systemd arranges.

The token is real: a password grant to dex, signed with dex's key, verified by
PostgREST against dex's published JWKS. The only thing this file mints is the
`user_account` row that says who that subject is, which is what provisioning an
OIDC user means.
"""

from uml_runner import run_test

# dex is one Go binary with its storage in memory, so it is up in about a
# second; postgres and the schema load are the rest. A minute is generous and
# the failure mode without a cap is a guest that hangs rather than fails.
READY = 120

WIKI = "http://127.0.0.1:8080"
REST = "http://127.0.0.1:3000"
# 127.0.0.2 rather than 127.0.0.1: dex answers on both inside the guest,
# and this is the address it calls itself, so `iss` matches what a browser
# on the host would be given. See the forward in test/default.nix.
DEX = "http://127.0.0.2:5556/dex"


async def curl(vm, args: str, *, timeout: float = 30) -> str:
    return await vm.succeed(f"curl -sS --max-time 10 {args}", timeout=timeout)


async def test(vms):
    vm, settings = vms.node, vms.settings

    # --- the units are what they claim to be ------------------------------
    await vm.wait_for_unit("postgresql.service")
    await vm.wait_for_unit("dex.service")
    await vm.wait_for_unit("fswiki.service")
    print("[test] postgresql, dex and fswiki are up")

    # `fswiki.service` being active only means systemd started the process. The
    # server migrates and starts a PostgREST before it binds, so the port is the
    # first honest answer.
    #
    # The journal on the way out, because a timeout here says only that nothing
    # answered. Which of the three phases it died in -- the schema, PostgREST,
    # or the socket -- is in the unit's log and nowhere else, and a run that
    # costs a guest boot should not have to be repeated to find out.
    try:
        await vm.succeed(
            f"until curl -sf --max-time 2 {WIKI}/ >/dev/null; do sleep 1; done",
            timeout=READY,
        )
    except Exception:
        print(f"[test] fswiki did not bind {WIKI}; its journal follows")
        print(await vm.journal("fswiki"))
        raise
    print("[test] the wiki answers")

    # --- the module put the right user on each connection ------------------
    #
    # The check that matters most and is invisible from inside the program: two
    # connections, one OS user, two database users. If PostgREST were connected
    # as the owner every policy in 050_rls.sql would be bypassed and every test
    # below would still pass.
    # Only PostgREST is still connected. The migrate phase used the other user
    # and closed, which is the shape wanted: DDL happens once, at start, and
    # what stays open for the life of the service cannot do it.
    who = await vm.succeed(
        "psql -U postgres -qAt -d fswiki -c "
        "\"select usename, count(*) from pg_stat_activity "
        "where datname = 'fswiki' and usename is not null "
        "and usename <> 'postgres' group by usename order by usename\""
    )
    users = {line.split("|")[0] for line in who.splitlines() if line}
    assert users == {"fswiki_authenticator"}, who
    print(f"[test] the open connections are {sorted(users)}")

    # And the migrate phase really was somebody else: the tables belong to the
    # other user. Both halves are needed. Ownership alone would not say who is
    # connected now, and the connection alone would not say the owner exists.
    owner = (await vm.succeed(
        "psql -U postgres -qAt -d fswiki -c "
        "\"select tableowner from pg_tables where schemaname = 'wiki' limit 1\""
    )).strip()
    assert owner == "fswiki", f"wiki tables belong to {owner!r}"
    print(f"[test] the tables belong to {owner}")

    ddl = (await vm.succeed(
        "psql -U postgres -qAt -d fswiki -c "
        "\"select has_database_privilege('fswiki_authenticator', 'fswiki', 'CREATE')\""
    )).strip()
    assert ddl == "f", f"the authenticator can create: {ddl}"
    print("[test] and the authenticator has no DDL")

    # --- somebody to be ----------------------------------------------------
    #
    # What the module deploys is an empty wiki: migrate loads the schema and the
    # seed, which builds the root document and the built-in roles. There is no
    # bob and no engineering tree in a real deployment. These are the suite's
    # own fixtures, loaded here so that what bob can read means the same thing
    # in this test as in every other.
    for name in ("fixtures", "seed"):
        await vm.succeed(
            f"psql -U postgres -qAt -d fswiki -v ON_ERROR_STOP=1 -f {settings[name]}",
            timeout=120,
        )
    print("[test] fixtures loaded")

    # --- a real token, from a real provider --------------------------------
    discovery = await curl(vm, f"{DEX}/.well-known/openid-configuration")
    issuer = await vm.succeed(
        f"curl -sS {DEX}/.well-known/openid-configuration | jq -r .issuer")
    issuer = issuer.strip()
    assert issuer == DEX, f"dex calls itself {issuer!r}: {discovery}"

    # A password grant, which is dex's resource-owner flow: no browser, no
    # redirect, and the only reason this test needs neither. `groups` has to be
    # asked for -- it is a scope, not something every token carries.
    await vm.succeed(
        f"curl -sS -X POST {DEX}/token"
        " -d grant_type=password"
        " -d client_id=fswiki -d client_secret=fswiki-test-secret"
        " -d username=bob@test.invalid -d password=hunter2"
        " -d scope='openid groups email profile'"
        " -o /tmp/grant.json"
    )
    token = (await vm.succeed("jq -r .id_token /tmp/grant.json")).strip()
    assert token and token != "null", await vm.succeed("cat /tmp/grant.json")

    # The subject from dex rather than from the token: userinfo is what a
    # relying party would ask, and it saves decoding base64url in a shell.
    access = (await vm.succeed("jq -r .access_token /tmp/grant.json")).strip()
    subject = (await vm.succeed(
        f"curl -sS -H 'Authorization: Bearer {access}' {DEX}/userinfo | jq -r .sub"
    )).strip()
    assert subject and subject != "null", "dex named no subject"
    print(f"[test] dex issued a token for {subject}")

    # --- before provisioning, the holder is nobody -------------------------
    #
    # The signature is good and PostgREST accepts it. There is simply no row
    # saying who this subject is, and an authenticated request that is nobody
    # must see what nobody sees.
    before = await curl(
        vm, f"-H 'Authorization: Bearer {token}' '{REST}/rpc/authenticated_user_id'")
    assert before.strip() == "null", before
    print("[test] an unprovisioned subject is nobody")

    # --- provision, and it is bob ------------------------------------------
    await vm.succeed(
        "psql -U postgres -qAt -d fswiki -v ON_ERROR_STOP=1 -c "
        f"\"update wiki.user_account set oidc_issuer = '{issuer}', "
        f"oidc_subject = '{subject}' "
        "where principal_id = (select id from wiki.principal "
        "where kind = 'user' and name = 'bob')\""
    )
    print("[test] bob's account now names dex")

    after = (await curl(
        vm, f"-H 'Authorization: Bearer {token}' '{REST}/rpc/authenticated_user_id'"
    )).strip().strip('"')
    expected = (await vm.succeed(
        "psql -U postgres -qAt -d fswiki -c "
        "\"select id from wiki.principal where kind = 'user' and name = 'bob'\""
    )).strip()
    assert after == expected, f"{after!r} is not bob ({expected!r})"
    print("[test] a dex token resolves to bob")

    # --- and it reads what bob reads ---------------------------------------
    #
    # Which proves the role mapping as well as the identity: PostgREST only got
    # to fswiki_user by reading `.groups[0]` out of a token that has no `role`
    # claim at all.
    paths = await curl(
        vm, f"-H 'Authorization: Bearer {token}' "
            f"'{REST}/current_document?select=path&order=path'")
    assert "root.engineering" in paths, paths
    print("[test] and reads bob's tree through the group claim")

    # --- the browser server takes the same token in a cookie ----------------
    page = await curl(
        vm, f"--cookie 'fswiki_session={token}' '{WIKI}/engineering/onboarding'")
    assert "<html" in page.lower(), page[:400]
    anonymous = await curl(vm, f"'{WIKI}/engineering/onboarding'")
    assert anonymous != page
    print("[test] the browser server serves it, and serves a visitor less")


run_test(test)
