{ lib
, python3Packages
, callPackage
}:

let
  fswiki-core = callPackage ../core { };
in
python3Packages.buildPythonPackage {
  # buildPythonPackage and not buildPythonApplication, though it has a
  # console script and is one. An application does not propagate for import,
  # and the test suite drives the ASGI app in-process rather than over a
  # socket -- which is the right way to test it, so this is the packaging that
  # allows it. The script still lands in $out/bin either way.
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
    python3Packages.uvicorn
    # Both optional to uvicorn and both worth having: the loop and the HTTP
    # parser are the two places a pure-python default costs something
    # measurable per request.
    python3Packages.uvloop
    python3Packages.httptools
  ] ++ fswiki-core.optional-dependencies.render;

  postInstall = ''
    cp -r ${./schema} $out/${python3Packages.python.sitePackages}/fswiki_server/schema
  '';

  pythonImportsCheck = [
    "fswiki_server" "fswiki_server.config" "fswiki_server.migrate"
    "fswiki_server.postgrest" "fswiki_server.app"
  ];

  meta = {
    description = "Read the wiki in a browser";
    mainProgram = "fswiki-serve";
  };
}
