{ lib
, python3Packages
, callPackage
}:

let
  fswiki-core = callPackage ../core { };
in
python3Packages.buildPythonPackage {
  pname = "fswiki-server";
  version = "0.1.0";
  pyproject = true;

  # schema/ comes along: the migrate phase reads it, and a server that has to
  # be told where its own schema lives is one more thing to get wrong in a
  # deployment. fswiki_server.config looks beside the package first.
  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = [
    fswiki-core
    python3Packages.psycopg
  ];

  postInstall = ''
    cp -r ${./schema} $out/${python3Packages.python.sitePackages}/fswiki_server/schema
  '';

  pythonImportsCheck = [
    "fswiki_server" "fswiki_server.config" "fswiki_server.migrate"
    "fswiki_server.postgrest"
  ];

  meta.description = "Read the wiki in a browser";
}
