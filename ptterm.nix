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
        rev = "679b70541a1239cc4f0f88ec6390d7c900eb4141";
        hash = "sha256-UgKajh/zp34lHbfq45p2jjmtIXaLXjy5bJnRPErgoFs=";
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
