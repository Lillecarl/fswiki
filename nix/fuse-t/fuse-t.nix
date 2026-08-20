{
  lib,
  stdenvNoCC,
  fetchurl,
  xar,
  cpio,
  gzip,
}:
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "fuse-t";
  version = "1.2.7";

  src = fetchurl {
    url = "https://github.com/macos-fuse-t/fuse-t/releases/download/${finalAttrs.version}/fuse-t-macos-installer-${finalAttrs.version}.pkg";
    hash = "sha256-ainHR+YahqQFoYnvw95CgS1zFHE1+TobsGJMHnuQ5lQ=";
  };

  dontUnpack = true;
  # The FSKit app extension is code signed. Its dependencies are either system
  # frameworks or @rpath-relative, so leave the signed Mach-O payload intact.
  dontFixup = true;

  nativeBuildInputs = [
    xar
    cpio
    gzip
  ];

  installPhase = ''
    runHook preInstall

    mkdir installer core fskit
    xar -xf "$src" -C installer

    (
      cd core
      gzip -dc ../installer/fuse-t-core.pkg/Payload | cpio -idm
    )
    (
      cd fskit
      gzip -dc ../installer/fuse-t-fskit.pkg/Payload | cpio -idm
    )

    install -Dm755 \
      "core/Library/Application Support/fuse-t/bin/go-nfsv4-${finalAttrs.version}" \
      "$out/bin/go-nfsv4-${finalAttrs.version}"
    ln -s "go-nfsv4-${finalAttrs.version}" "$out/bin/go-nfsv4"

    mkdir -p "$out/share"
    cp -R "core/Library/Application Support/fuse-t" "$out/share/fuse-t"
    cp -R "core/Library/Frameworks" "$out/Frameworks"
    cp -R "fskit/Applications" "$out/Applications"

    runHook postInstall
  '';

  meta = {
    description = "Kext-less FUSE implementation for macOS";
    homepage = "https://www.fuse-t.org/";
    license = lib.licenses.gpl2Only;
    platforms = lib.platforms.darwin;
  };
})
