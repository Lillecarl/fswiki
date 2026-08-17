{ lib
, python3Packages
, callPackage
}:

let
  fswiki-core = callPackage ../core { };
in
python3Packages.buildPythonApplication {
  pname = "fswiki-cli";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = [
    fswiki-core
    python3Packages.anyio
  ];

  pythonImportsCheck = [ "fswiki_cli" "fswiki_cli.report" "fswiki_cli.paths" ];

  meta = {
    description = "Publish fswiki drafts";
    mainProgram = "fswiki";
  };
}
