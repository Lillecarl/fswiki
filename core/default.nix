{ lib
, python3Packages
}:

python3Packages.buildPythonPackage {
  pname = "fswiki-core";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.httpx python3Packages.merge3 ];

  pythonImportsCheck = [
    "fswiki_core" "fswiki_core.client" "fswiki_core.naming" "fswiki_core.merge"
  ];

  meta.description = "PostgREST access and path naming, shared by the fswiki clients";
}
