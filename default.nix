{
  pkgs ? import <nixpkgs> { },
}:
{
  # PostgREST access and path naming, shared by both clients so that publishing
  # a draft does not require the ability to mount a filesystem.
  core = pkgs.callPackage ./core { };
  cli = pkgs.callPackage ./cli { };
  fuse = pkgs.callPackage ./fuse { };
  # Python bindings for Apple's macOS 15+ FSKit framework. Kept separate from
  # the Linux FUSE client so both filesystem frontends can evolve independently.
  pyobjc-framework-FSKit = pkgs.callPackage ./nix/pyobjc-framework-FSKit.nix { };
  # The browser-facing reader, and the schema it loads on startup. Not a
  # client: it holds no identity, passes a visitor's token through, and lets
  # Postgres decide what comes back.
  server = pkgs.callPackage ./server { };

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
