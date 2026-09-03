{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  flit-core,
  wcwidth,
  # Set this to build against a local working copy, e.g. ../pyte.
  localSrc ? null,
}:

buildPythonPackage {
  pname = "pyte";
  version = "0.8.3.dev";
  pyproject = true;

  build-system = [ flit-core ];

  src =
    if localSrc != null then
      localSrc
    else
      fetchFromGitHub {
        owner = "Lillecarl";
        repo = "pyte";
        # The APC, DCS and CSI parsing that the kitty protocols need
        # only lives on this fork.
        rev = "13df028dd7fe2a0de1f47d0bef1ebd04fa37a59b";
        hash = "sha256-ZqQZD+LGRIJOB6O0tLEfsD7s1g6Ci4RW74V46A/JnQU=";
      };

  dependencies = [ wcwidth ];

  doCheck = false;
  pythonImportsCheck = [ "pyte" ];

  meta = {
    description = "Simple in-memory VTXXX-compatible terminal emulator";
    homepage = "https://github.com/selectel/pyte";
    license = lib.licenses.lgpl3;
  };
}
