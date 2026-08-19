{
  pkgs ? import <nixpkgs> { },
}:
{
  # PostgREST access and path naming, shared by both clients so that publishing
  # a draft does not require the ability to mount a filesystem.
  core = pkgs.callPackage ./core { };
  cli = pkgs.callPackage ./cli { };
  fuse = pkgs.callPackage ./fuse { };

  # Local Postgres + PostgREST under process-compose. Not a component of the
  # product; it is what the components are developed against.
  dev = import ./dev { inherit pkgs; };
  # The test suite, and everything it shells out to. `nix run --file . tests`.
  tests = import ./test { inherit pkgs; };
  # Scratch interpreter for poking at a running dev stack. Mirrors the runtime
  # deps of the fuse client so `nix run --file . python` can import them.
  python = pkgs.python3.withPackages (
    ps: with ps; [
      pyfuse3
      trio
      anyio
      httpx
    ]
  );
}
