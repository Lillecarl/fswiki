{
  lib,
  stdenv,
  fetchFromGitHub,
  cmake,
  libiconv,
  apple-sdk_15,
}:
stdenv.mkDerivation {
  pname = "libfuse-t";
  version = "unstable-2026-07-15";

  src = fetchFromGitHub {
    owner = "macos-fuse-t";
    repo = "libfuse";
    rev = "4eb26b4b45cd9a1baf5e846955d43b76de1c7be3";
    hash = "sha256-lfsZ/hEwNQSJKxrSZ2RqXgLS+BBKWevzb4HNqZ+rZgo=";
  };

  nativeBuildInputs = [ cmake ];
  buildInputs = [
    apple-sdk_15
    libiconv
  ];

  preConfigure = ''
    export CLANG_MODULE_CACHE_PATH="$TMPDIR/clang-module-cache"
    mkdir -p "$CLANG_MODULE_CACHE_PATH"
  '';

  # Upstream unconditionally requests a universal binary. A Nix build targets
  # one host platform, so leave architecture selection to the toolchain.
  postPatch = ''
    substituteInPlace lib/CMakeLists.txt \
      --replace-fail "set(CMAKE_OSX_ARCHITECTURES arm64;x86_64)" ""
    substituteInPlace CMakeLists.txt \
      --replace-fail "add_subdirectory(example)" ""
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 lib/libfuse-t.dylib "$out/lib/libfuse-t.dylib"
    cp -R ../include "$out/include"
    runHook postInstall
  '';

  meta = {
    description = "FUSE-T's libfuse 2-compatible client library";
    homepage = "https://github.com/macos-fuse-t/libfuse";
    license = lib.licenses.gpl2Only;
    platforms = lib.platforms.darwin;
  };
}
