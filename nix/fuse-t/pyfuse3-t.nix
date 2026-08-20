{
  lib,
  python3Packages,
  callPackage,
}:

let
  libfuse3-t = callPackage ./libfuse3-t.nix { };
in
(python3Packages.pyfuse3.override { fuse3 = libfuse3-t; }).overridePythonAttrs (old: {
  # The upstream package is marked Linux-only because nixpkgs normally has no
  # Darwin libfuse3. FUSE-T supplies the compatible library here.
  meta = old.meta // {
    platforms = lib.platforms.darwin;
  };

  # Upstream's integration tests assume Linux mount helpers. Import checks are
  # retained and exercise the compiled extension against libfuse3-t.
  doCheck = false;

  # pyfuse3 otherwise includes Linux's fs.h unconditionally just to obtain
  # these two FUSE rename flag values. They are part of the FUSE protocol and
  # have the same fixed values on FUSE-T.
  patches = (old.patches or [ ]) ++ [ ./pyfuse3-darwin.patch ];
})
