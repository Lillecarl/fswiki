{
  pkgs ? import <nixpkgs> { },
}:
{
  cli = pkgs.callPackage ./cli { };
  fuse = pkgs.callPackage ./fuse { };
  server = pkgs.callPackage ./server { };

  # Local Postgres + PostgREST under process-compose. Not a component of the
  # product; it is what the components are developed against.
  dev = import ./dev { inherit pkgs; };
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
