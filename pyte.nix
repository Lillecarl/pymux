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
        rev = "3a6779f06420056f20415c528f9170ecb3952943";
        hash = "sha256-DaA00vH0A4jkXWwLEjDMt4vURxfkskx2Rg2QxI4VwWM=";
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
