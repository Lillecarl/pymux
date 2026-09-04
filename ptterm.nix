{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  prompt-toolkit,
  pyte,
  wcwidth,
  # Set this to build against a local working copy, e.g. ../ptterm.
  localSrc ? null,
}:

buildPythonPackage {
  pname = "ptterm";
  version = "0.2-unstable-2026-09-03";
  format = "setuptools";

  src =
    if localSrc != null then
      localSrc
    else
      fetchFromGitHub {
        owner = "Lillecarl";
        repo = "ptterm";
        # Upstream has no release that works with prompt_toolkit 3, and
        # the kitty protocol support only lives on this fork.
        rev = "bae9ebe457b25033f7a45f941e2659cbc6307be1";
        hash = "sha256-KCPwQ/vXCUDV14Iv6LiX85iNyqmB74xShoI8Iqw4iQQ=";
      };

  propagatedBuildInputs = [
    prompt-toolkit
    pyte
    wcwidth
  ];

  doCheck = false;
  pythonImportsCheck = [ "ptterm" ];

  meta = {
    description = "Terminal emulator widget for prompt_toolkit";
    homepage = "https://github.com/prompt-toolkit/ptterm";
    license = lib.licenses.bsd3;
  };
}
