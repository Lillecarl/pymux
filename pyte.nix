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
        rev = "34ca97a73ddece1ad7ae0b3dc0c7ec9d73104556";
        hash = "sha256-8QiJFTEledyHQ/0VBAqszJg2ulxOvk2qFY0izBGbiUo=";
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
