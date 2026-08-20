{
  lib,
  stdenv,
  fetchFromGitHub,
  meson,
  ninja,
  pkg-config,
  libiconv,
  apple-sdk_15,
}:
stdenv.mkDerivation {
  pname = "libfuse3-t";
  version = "3.19.0-rc0-unstable-2026-03-31";

  src = fetchFromGitHub {
    owner = "macos-fuse-t";
    repo = "libfuse3";
    rev = "6a245daa2b914e6c05fafe687c0aaef7cb3deb20";
    hash = "sha256-YyiQsCaIjCWQBAqqISQVASzQXCqtQa7S/LAHMxRYrJA=";
  };

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
  ];
  buildInputs = [
    apple-sdk_15
    libiconv
  ];

  preConfigure = ''
    export CLANG_MODULE_CACHE_PATH="$TMPDIR/clang-module-cache"
    mkdir -p "$CLANG_MODULE_CACHE_PATH"
  '';

  mesonFlags = [
    "-Dexamples=false"
    "-Dinitscriptdir="
    "-Dtests=false"
    "-Duseroot=false"
    "-Dutils=false"
  ];

  meta = {
    description = "FUSE-T's libfuse 3-compatible client library";
    homepage = "https://github.com/macos-fuse-t/libfuse3/tree/fuse-t";
    license = lib.licenses.lgpl2Plus;
    platforms = lib.platforms.darwin;
  };
}
