# The package this repository builds. Nothing else belongs here: the dev shell
# and the collection that assembles this with its siblings live in pyterm.
#
# prompt-toolkit and ptterm arrive as arguments, so nixpkgs supplies them when
# this repository is built on its own, and pyterm supplies the sibling
# checkouts when it builds the collection.
{
  lib,
  buildPythonPackage,
  setuptools,
  prompt-toolkit,
  ptterm,
  docopt-ng,
}:
buildPythonPackage {
  pname = "pymux";
  version = "0.15";
  src = lib.cleanSource ./.;
  pyproject = true;

  # pymux carries no pyproject.toml, so the build backend is named here.
  build-system = [ setuptools ];
  dependencies = [
    prompt-toolkit
    ptterm
    # docopt-ng is the maintained fork, and imports as docopt.
    docopt-ng
  ];

  pythonImportsCheck = [ "pymux" ];

  meta = {
    description = "Terminal multiplexer built on prompt_toolkit";
    homepage = "https://github.com/prompt-toolkit/pymux";
    license = lib.licenses.bsd3;
    mainProgram = "pymux";
  };
}
