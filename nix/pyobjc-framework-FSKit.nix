{
  lib,
  python3Packages,
  darwin,
  apple-sdk_15,
}:

python3Packages.buildPythonPackage rec {
  pname = "pyobjc-framework-FSKit";
  pyproject = true;

  # PyObjC framework packages must be kept at exactly the same release as the
  # bridge and Cocoa packages. Reusing pyobjc-core's monorepo source gives us
  # that invariant without maintaining a second PyPI version and hash here.
  inherit (python3Packages.pyobjc-core) version src;

  patches = python3Packages.pyobjc-core.patches or [ ];
  sourceRoot = "${src.name}/pyobjc-framework-FSKit";

  build-system = [ python3Packages.setuptools ];

  buildInputs = [
    apple-sdk_15
    darwin.libffi
  ];
  nativeBuildInputs = [ darwin.DarwinTools ];

  # PyObjC 11.1 calls Apple tools by absolute path and uses option spellings
  # that Nix's DarwinTools compatibility wrappers do not accept.
  postPatch = ''
    substituteInPlace pyobjc_setup.py \
      --replace-fail "-buildversion" "-buildVersion" \
      --replace-fail "-productversion" "-productVersion" \
      --replace-fail "/usr/bin/sw_vers" "sw_vers" \
      --replace-fail "/usr/bin/xcrun" "xcrun"
  '';

  dependencies = [
    python3Packages.pyobjc-core
    python3Packages.pyobjc-framework-Cocoa
  ];

  env.NIX_CFLAGS_COMPILE = toString [
    "-I${darwin.libffi.dev}/include"
    "-Wno-error=unused-command-line-argument"
  ];

  pythonImportsCheck = [ "FSKit" ];

  # FSKit was introduced in macOS 15. The package can be compiled only when
  # that framework is present in the SDK, and importing it needs the framework
  # on the host system as well.
  meta = {
    description = "PyObjC wrappers for Apple's FSKit framework";
    homepage = "https://pypi.org/project/pyobjc-framework-FSKit/";
    license = lib.licenses.mit;
    platforms = lib.platforms.darwin;
  };
}
