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

  # Rendering is optional to *import* and required to *run*: each backend
  # registers only if its library is there, so a build without these still
  # gives you a working client, just not a renderer.
  optional-dependencies.render = [
    python3Packages.nh3
    python3Packages.markdown-it-py
    python3Packages.mistune
    python3Packages.docutils
  ];

  pythonImportsCheck = [
    "fswiki_core" "fswiki_core.client" "fswiki_core.naming" "fswiki_core.merge"
    "fswiki_core.render" "fswiki_core.render.links" "fswiki_core.render.registry"
    "fswiki_core.pages"
  ];

  meta.description = "PostgREST access and path naming, shared by the fswiki clients";
}
