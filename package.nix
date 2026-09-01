{
  lib,
  buildPythonApplication,
  pythonOlder,
  prompt-toolkit,
  ptterm,
  docopt-ng,
}:

buildPythonApplication {
  pname = "pymux";
  version = "0.15";
  format = "setuptools";

  src = ./.;

  disabled = pythonOlder "3.11";

  propagatedBuildInputs = [
    prompt-toolkit
    ptterm
    docopt-ng
  ];

  doCheck = false;
  pythonImportsCheck = [ "pymux" ];

  meta = {
    description = "Pure Python terminal multiplexer (tmux alternative)";
    homepage = "https://github.com/prompt-toolkit/pymux";
    license = lib.licenses.bsd3;
    mainProgram = "pymux";
    platforms = lib.platforms.unix;
  };
}
