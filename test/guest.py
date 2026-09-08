#!/usr/bin/env python3
"""The mount half of the suite, in a guest that has a /dev/fuse.

`tests.check` runs everything a build sandbox can run, which is everything
except the mounts: the sandbox's /dev holds null, zero, random and little else,
and no amount of unsharing conjures a device node that is not there. A UML
guest is a kernel of its own, so it has the device, and this runs the same
`fswiki-test` against it with nothing rewritten.

Only `-m mount` here. The rest already runs in `check`, faster and without a
guest, and running it twice would say nothing new.
"""

from uml_runner import run_test

# The suite builds a Postgres cluster, loads the schema and then mounts several
# filesystems. That is minutes, not the two of the default.
TIMEOUT = 1800


async def test(vms):
    vm, settings = vms.node, vms.settings

    await vm.succeed("test -c /dev/fuse")

    # HOME because pytest and initdb both want one, and the agent's environment
    # has none. FSWIKI_ROOT because the runner otherwise walks up from its
    # working directory looking for server/schema, and there is no checkout
    # under / to find.
    out = await vm.succeed(
        "env"
        " HOME=/root"
        f" SSL_CERT_FILE={settings['certs']}"
        f" FSWIKI_ROOT={settings['root']}"
        f" {settings['runner']} -m mount -p no:cacheprovider",
        timeout=TIMEOUT,
    )
    print(f"[test] {out}")


run_test(test)
