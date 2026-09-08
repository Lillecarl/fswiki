# The packages, as an overlay.
#
# `default.nix` builds them for someone standing in this checkout. An overlay is
# for everyone else: a NixOS configuration that imports ./module.nix wants
# `pkgs.fswiki-server` to exist, and a module that reached sideways into a
# relative path would only work from here.
#
#     nixpkgs.overlays = [ (import (fswiki + "/nix/overlay.nix")) ];
#     imports = [ (fswiki + "/nix/module.nix") ];
final: prev: {
  fswiki-core = final.callPackage ../core { };
  fswiki-cli = final.callPackage ../cli { };
  fswiki-fuse = final.callPackage ../fuse { };
  fswiki-server = final.callPackage ../server { };
}
