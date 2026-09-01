{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  prompt-toolkit,
  pyte,
  wcwidth,
}:

buildPythonPackage {
  pname = "ptterm";
  version = "0.2-unstable-2026-08-09";
  format = "setuptools";

  src = fetchFromGitHub {
    owner = "prompt-toolkit";
    repo = "ptterm";
    rev = "master";
    hash = "sha256-ZiBvLcKyQ84QaD6X7kfn1JMoPGc9iJZOoji+hh9JpWY=";
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
