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
in
pkgs.writeShellApplication {
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
}
