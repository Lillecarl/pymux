{
  lib,
  buildPythonApplication,
  pythonOlder,
  prompt-toolkit,
  ptterm,
  docopt-ng,
  makeWrapper,
  terminfo ? null,
}:

buildPythonApplication {
  pname = "pymux";
  version = "0.15";
  format = "setuptools";

  src = ./.;

  disabled = pythonOlder "3.11";

  nativeBuildInputs = lib.optional (terminfo != null) makeWrapper;

  # Where the entry that describes a pane lives. A pane that finds it
  # says `TERM=pymux`; one that does not falls back to xterm.
  makeWrapperArgs = lib.optionals (terminfo != null) [
    "--set-default"
    "PYMUX_TERMINFO"
    "${terminfo}/share/terminfo"
  ];

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
