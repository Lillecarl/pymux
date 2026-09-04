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
        owner = "Lillecarl";
        repo = "python-prompt-toolkit";
        # A render of pymux spends about 80% of its cpu in
        # prompt_toolkit. The cheaper render only lives on this fork,
        # which sits on the 3.0.52 release that nixpkgs carries.
        rev = "47cb62005f3b1c8e334397cb020f18c1f24e16bc";
        hash = "sha256-r9YLs8To4b2by+/5E+zY83xx8VKVt671/uV9zsRcveI=";
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
