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
        rev = "c85d50559e53c4d55ff9aab15c13211761c23333";
        hash = "sha256-T21BJW66RJg1VDPOM7wCfkSRr9T5kr0w1/TKbTUD/7U=";
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
