{ pkgs ? import <nixpkgs> { } }:

# The test runner, as a program rather than a shell full of instructions.
#
#   nix run --file . tests                  # everything
#   nix run --file . tests -- -k impersonation -x
#
# Everything the suite shells out to is put on PATH here, so conftest can fail
# with "not on PATH" instead of with whatever a missing initdb looks like three
# layers down.

let
  inherit (pkgs) lib;

  fswiki-core = pkgs.callPackage ../core { };
  fswiki-cli = pkgs.callPackage ../cli { };
  fswiki-fuse = pkgs.callPackage ../fuse { };

  python = pkgs.python3.withPackages (ps: [
    ps.pytest
    # The async plugin. anyio, not pytest-asyncio: fswiki_core.client is
    # written against anyio precisely so the same code runs under trio in the
    # mount and under asyncio in the CLI, and only this plugin can run a test
    # under either.
    ps.anyio
    ps.trio
    ps.pytest-xdist
    fswiki-core
    ps.httpx
    ps.nh3
    ps.markdown-it-py
    ps.mistune
  ]);
  runner = pkgs.writeShellApplication {
    name = "fswiki-test";

    runtimeInputs = [
      python
      pkgs.postgresql_18
      pkgs.postgrest
      fswiki-cli
      fswiki-fuse
      # getfattr, for the xattrs the mount exposes.
      pkgs.attr
      # fusermount3 is setuid and must come from the system; see fuse/default.nix
      # for why this is a suffix everywhere it appears.
      pkgs.fuse3
    ];

    text = ''
      # The mount's own library, from the package that is actually installed
      # rather than from the source tree beside it. `fswiki_fuse.audit` is
      # ordinary Python -- a queue, a cap and some arithmetic -- and it can be
      # tested without a filesystem, in a build sandbox, at unit-test speed. It
      # imports anyio and fswiki_core and nothing that needs /dev/fuse, which is
      # why PYTHONPATH is enough and pyfuse3 is not in the environment above.
      export PYTHONPATH=${fswiki-fuse}/${pkgs.python3.sitePackages}''${PYTHONPATH:+:$PYTHONPATH}

      root=''${FSWIKI_ROOT:-$PWD}
      while [ ! -d "$root/server/sql" ] && [ "$root" != / ]; do
        root=$(dirname "$root")
      done
      if [ ! -d "$root/server/sql" ]; then
        echo "fswiki-test: not inside an fswiki checkout; set FSWIKI_ROOT" >&2
        exit 1
      fi
      cd "$root"
      exec pytest "$@"
    '';

    meta.description = "Run the fswiki test suite against a throwaway stack";
  };

  # The half of the suite that runs in a build sandbox.
  #
  #   nix build --file . tests.check -L
  #
  # Measured in the sandbox rather than assumed: `lo` is up with 127.0.0.1 and
  # binds and connects fine, so Postgres and PostgREST over loopback need
  # nothing special -- Unix sockets would buy nothing here, though PostgREST
  # does support them (server-unix-socket).
  #
  # What is genuinely absent is `/dev/fuse`. The sandbox's /dev has null, zero,
  # random, tty and little else, and no amount of unsharing conjures a device
  # node that is not there, so the mount tests cannot run in a build at all.
  # `-m 'not mount'` is exactly the line between the two, and that is what the
  # marker is for.
  check = pkgs.runCommand "fswiki-check" {
    nativeBuildInputs = [ runner ];
    src = lib.cleanSource ../.;
    # httpx builds a default SSL context even for an http:// URL, and looking
    # for a CA bundle that is not there fails with a bare FileNotFoundError
    # from ssl.py -- nothing to do with this suite, but it takes out every test
    # that uses the client.
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  } ''
    export FSWIKI_ROOT=$src
    export HOME=$TMPDIR
    fswiki-test -m 'not mount' -p no:cacheprovider
    touch $out
  '';
in
# The runner is the default -- `nix run --file . tests` -- with the sandboxed
# build hanging off it as `tests.check`.
runner // { inherit check; }
