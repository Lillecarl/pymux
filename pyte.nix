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
        rev = "4f3bf621602143e020948626b27ac52ba048389c";
        hash = "sha256-ZC82JBqLi2xrJ7RB89IrLaJNWyAtn0kTB1+lL1PYRt0=";
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
