{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  setuptools,
  wcwidth,
  # Set this to build against a local working copy, e.g. ../prompt-toolkit.
  localSrc ? null,
}:

buildPythonPackage {
  pname = "prompt-toolkit";
  version = "3.0.52-unstable-2026-09-03";
  pyproject = true;

  src =
    if localSrc != null then
      localSrc
    else
      fetchFromGitHub {
        owner = "prompt-toolkit";
        repo = "python-prompt-toolkit";
        # The same release that nixpkgs carries. A local working copy
        # next to this repo takes its place, which is how a change to
        # the render of prompt_toolkit gets measured.
        tag = "3.0.52";
        hash = "sha256-ggCy7xTvOkjy6DgsO/rPNtQiAQ4FjsK4ShrvkIHioNQ=";
      };

  postPatch = ''
    # The version comes from the metadata of an installed package, which
    # a source build does not have.
    substituteInPlace src/prompt_toolkit/__init__.py \
      --replace-fail 'metadata.version("prompt_toolkit")' '"3.0.52"'
  '';

  build-system = [ setuptools ];

  dependencies = [ wcwidth ];

  doCheck = false;
  pythonImportsCheck = [ "prompt_toolkit" ];

  meta = {
    description = "Library for building powerful interactive command lines";
    homepage = "https://github.com/prompt-toolkit/python-prompt-toolkit";
    license = lib.licenses.bsd3;
  };
}
