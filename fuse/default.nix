{
  lib,
  python3Packages,
  fuse3,
  makeWrapper,
  callPackage,
  stdenv,
}:

let
  fuse-t = callPackage ../nix/fuse-t/fuse-t.nix { };
  pyfuse3 =
    if stdenv.hostPlatform.isDarwin then
      callPackage ../nix/fuse-t/pyfuse3-t.nix { }
    else
      python3Packages.pyfuse3;
in
python3Packages.buildPythonApplication {
  pname = "fswiki-fuse";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = [
    (callPackage ../core { })
    pyfuse3
    python3Packages.trio
    python3Packages.anyio
  ];

  nativeBuildInputs = [ makeWrapper ];

  # pyfuse3 links libfuse3, but `fusermount3` is a separate binary the library
  # shells out to at mount and unmount time, and it must be **setuid** — an
  # unprivileged mount needs CAP_SYS_ADMIN.
  #
  # `--suffix`, emphatically not `--prefix`: nothing in the Nix store can be
  # setuid, so on NixOS the working binary is the wrapper in /run/wrappers/bin
  # and putting the store copy first shadows it. The result is a mount that
  # fails with "Operation not permitted" while `fusermount3 --version` works
  # fine. The store copy stays on the end as a fallback for non-NixOS hosts,
  # where the distro's own setuid copy is in /usr/bin and found first anyway.
  postFixup =
    if stdenv.hostPlatform.isDarwin then
      ''
        wrapProgram $out/bin/fswiki-mount \
          --set FUSE_NFSSRV_PATH ${fuse-t}/bin/go-nfsv4
      ''
    else
      ''
        wrapProgram $out/bin/fswiki-mount \
          --suffix PATH : ${lib.makeBinPath [ fuse3 ]}
      '';

  pythonImportsCheck = [
    "fswiki_fuse"
    "fswiki_fuse.fs"
    "fswiki_fuse.model"
    "fswiki_fuse.audit"
    "fswiki_fuse.procinfo"
  ];

  # The suite needs a live PostgREST; see fuse/test/run.sh.
  doCheck = false;

  meta = {
    description = "Mount an fswiki wiki as a filesystem";
    mainProgram = "fswiki-mount";
  };
}
