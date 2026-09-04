# The package this repository builds, and the tests that judge it. Nothing
# else belongs here: the dev shell and the collection that assembles this
# with its siblings live in pyterm.
#
# prompt-toolkit and ptterm arrive as arguments, so nixpkgs supplies them
# when this repository is built on its own, and pyterm supplies the sibling
# checkouts when it builds the collection.
{
  lib,
  buildPythonApplication,
  pythonOlder,
  prompt-toolkit,
  ptterm,
  docopt-ng,
  makeWrapper,
  python,
  pytest,
  hypothesis,
  wcwidth,
  runCommand,
  ncurses,
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

    # The suites run as `checks.pymux` and `checks.pty`, against the source.
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

  pythonWithTests = python.withPackages (ps: [
    ptterm
    prompt-toolkit
    docopt-ng
    hypothesis
    pytest
    wcwidth
  ]);

  # `PYMUX_TESTS` picks what pytest runs, for instance
  # `PYMUX_TESTS=tests/test_sixel_encoder.py nix-build -A pymux.checks.pymux`.
  # It reaches the evaluation through the environment, so it only works with
  # impure evaluation, which `nix-build` uses by default.
  selection =
    let
      value = builtins.getEnv "PYMUX_TESTS";
    in
    if value == "" then "tests" else value;

  # Only the module and the tests, not the whole repository. A copy of
  # everything makes the test runs rebuild on every unrelated edit.
  testSources = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pymux
      ./libpymux
      ./tests
    ];
  };

  # Run a test command in the build sandbox. Nothing of the run reaches the
  # machine: the sockets, the temporary directories and the processes all
  # live and die inside it.
  runInSandbox = name: command:
    runCommand name {
      nativeBuildInputs = [ pythonWithTests ];
      # Rerun whenever the selection changes.
      inherit selection;
    } ''
      cp -r ${testSources}/pymux ${testSources}/libpymux ${testSources}/tests .
      chmod -R +w .
      export HOME="$TMPDIR"
      export LANG=C.UTF-8
      export PYTHONDONTWRITEBYTECODE=1

      # The entry of terminfo that a pane is told about. The wrapper of the
      # package sets this; a test runs the source, so it sets it here.
      export PYMUX_TERMINFO=${terminfo}/share/terminfo
      ${command}
      touch "$out"
    '';

  checks = {
    # The unit tests of pymux.
    pymux = runInSandbox "pymux-tests" ''
      python -m pytest $selection -q -p no:cacheprovider
    '';

    # The end to end test. It opens a pty, starts a server and attaches a
    # client, so it needs a sandbox that gives it /dev/ptmx.
    pty = runInSandbox "pymux-pty-tests" ''
      python tests/drive_with_pty.py
    '';
  };
in
package
