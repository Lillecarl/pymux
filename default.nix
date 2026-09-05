# The package this repository builds. The suites that judge it live in
# `nix/checks.nix`, which declares its own inputs, so the terminal emulators,
# the display servers and the conformance suite that only a test needs are
# not named here.
#
# Nothing else belongs in this repository: the dev shell and the collection
# that assembles this with its siblings live in pyterm.
#
# prompt-toolkit and ptterm arrive as arguments, so nixpkgs supplies them
# when this repository is built on its own, and pyterm supplies the sibling
# checkouts when it builds the collection.
#
# mesa arrives as an argument as well, and only the checks use it: kitty
# draws with OpenGL and a build sandbox has no graphics card. It cannot come
# from the scope here, because this is the python package set and `mesa`
# there is a python binding that nixpkgs has marked broken.
{
  lib,
  buildPythonApplication,
  pythonOlder,
  prompt-toolkit,
  ptterm,
  docopt-ng,
  makeWrapper,
  python,
  runCommand,
  ncurses,
  callPackage,
  mesa,
}:
let
  package = buildPythonApplication {
    pname = "pymux";
    version = "0.15";
    format = "setuptools";

    src = lib.cleanSource ./.;

    disabled = pythonOlder "3.11";

    nativeBuildInputs = [ makeWrapper ];

    # Where the entry that describes a pane lives. A pane that finds it
    # says `TERM=pymux`; one that does not falls back to xterm.
    makeWrapperArgs = [
      "--set-default"
      "PYMUX_TERMINFO"
      "${terminfo}/share/terminfo"
    ];

    propagatedBuildInputs = [
      prompt-toolkit
      ptterm
      docopt-ng
    ];

    # The suites run as `checks.unit`, `checks.pty` and the rest, against the source.
    doCheck = false;
    pythonImportsCheck = [
      "pymux"
      "libpymux"
    ];

    passthru = { inherit checks terminfo; };

    meta = {
      description = "Pure Python terminal multiplexer (tmux alternative)";
      homepage = "https://github.com/prompt-toolkit/pymux";
      license = lib.licenses.bsd3;
      mainProgram = "pymux";
      platforms = lib.platforms.unix;
    };
  };

  # The terminfo entry that describes a pane, compiled from the table that
  # ptterm also answers XTGETTCAP with. A program built on ncurses reads the
  # database instead of asking, and without an entry of our own it reads the
  # one for xterm-256color and never writes a curly underline.
  terminfo = runCommand "pymux-terminfo" {
    nativeBuildInputs = [
      (python.withPackages (ps: [ ptterm ]))
      ncurses
    ];
  } ''
    mkdir -p $out/share/terminfo
    python -m ptterm.terminfo > pymux.ti
    tic -x -o $out/share/terminfo pymux.ti

    # An entry that does not compile leaves a pane naming a terminal that
    # is not there, which is worse than naming xterm.
    TERMINFO_DIRS=$out/share/terminfo: infocmp -x pymux > /dev/null
  '';

  # Only the module and the tests, not the whole repository. A copy of
  # everything makes the test runs rebuild on every unrelated edit.
  #
  # It is built here and not under `nix`, because `./.` there is the `nix`
  # directory and this needs the root of the repository.
  testSources = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pymux
      ./libpymux
      ./tests
    ];
  };

  # ptterm and prompt-toolkit go in by hand. They arrive here as arguments,
  # so the scope that `callPackage` fills from holds the ones of nixpkgs and
  # not the sibling checkouts that pyterm assembled.
  #
  # mesa arrives as an argument for the same reason, and pyterm passes the
  # one that draws. In this package set `mesa` is a python binding that
  # nixpkgs has marked broken, so it cannot be taken from the scope.
  checks = callPackage ./nix/checks.nix {
    inherit
      terminfo
      testSources
      ptterm
      prompt-toolkit
      docopt-ng
      mesa
      ;
  };
in
package
