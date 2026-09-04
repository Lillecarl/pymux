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
  stdenv,
  fetchFromGitHub,
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

  # The conformance suite of Thomas Dickey, after George Nachman wrote it for
  # iTerm2. It judges a terminal from the inside: it runs as a program in that
  # terminal, writes control sequences, and reads the reports that come back.
  #
  # Its modules import each other by plain name, so they have to sit on the
  # path together and not under a package directory of their own. `bin/esctest`
  # runs the suite; `share/esctest2` is where the check imports it from,
  # because the check drives it one test at a time.
  esctest2 = stdenv.mkDerivation {
    pname = "esctest2";
    version = "0-unstable-2025-08-24";

    src = fetchFromGitHub {
      owner = "ThomasDickey";
      repo = "esctest2";
      rev = "664be3cf2c1e3f06bc93a8bafb48a0db83c607db";
      hash = "sha256-JmUMvWmQoPyoWttW4K7Ap3/Tn0D3n8tHVPwprpeC+Is=";
    };

    nativeBuildInputs = [ makeWrapper ];
    dontConfigure = true;
    dontBuild = true;

    installPhase = ''
      runHook preInstall

      mkdir -p $out/share/esctest2
      cp -r esctest/. $out/share/esctest2/

      makeWrapper ${python.interpreter} $out/bin/esctest \
        --add-flags $out/share/esctest2/esctest.py

      runHook postInstall
    '';

    meta = {
      description = "Conformance tests for terminal emulators";
      homepage = "https://github.com/ThomasDickey/esctest2";
      license = lib.licenses.gpl2Only;
      mainProgram = "esctest";
      platforms = lib.platforms.unix;
    };
  };

  pythonWithTests = python.withPackages (ps: [
    ptterm
    prompt-toolkit
    docopt-ng
    hypothesis
    pytest
    wcwidth
  ]);

  # Three knobs that reach the evaluation through the environment. They work
  # because a build from a file evaluates impurely; a flake would see none of
  # them.

  # What pytest runs, for instance
  # `PYMUX_TESTS=tests/test_sixel_encoder.py nix build --file . checks.pymux`.
  selection =
    let
      value = builtins.getEnv "PYMUX_TESTS";
    in
    if value == "" then "tests" else value;

  # Which conformance tests run. It is a regular expression that the suite
  # matches against "Class.method", for instance
  # `PYMUX_ESCTEST_INCLUDE=BSTests nix build --file . checks.pymux-esctest`.
  esctestInclude =
    let
      value = builtins.getEnv "PYMUX_ESCTEST_INCLUDE";
    in
    if value == "" then ".*" else value;

  # Set `PYMUX_ESCTEST_RECORD` to anything and the conformance check writes
  # the list of tests that fail now instead of judging the run against the
  # recorded one. The result of the build is that list, ready to copy over
  # `tests/esctest-failures.txt`.
  esctestRecord = builtins.getEnv "PYMUX_ESCTEST_RECORD" != "";

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
  #
  # `inputs` adds to what the run may call. `env` names the variables that the
  # command reads, and a change to one of them rebuilds the check, which is
  # what makes the knobs above work.
  runInSandbox = { name, inputs ? [ ], env ? { } }: command:
    runCommand name (env // {
      nativeBuildInputs = [ pythonWithTests ] ++ inputs;
    }) ''
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
    pymux = runInSandbox {
      name = "pymux-tests";
      env = { inherit selection; };
    } ''
      python -m pytest $selection -q -p no:cacheprovider
    '';

    # The end to end test. It opens a pty, starts a server and attaches a
    # client, so it needs a sandbox that gives it /dev/ptmx.
    pty = runInSandbox { name = "pymux-pty-tests"; } ''
      python tests/drive_with_pty.py
    '';

    # The conformance suite, run in a pane. It is not a pass or fail of its
    # own: most of it fails, and each failure names a real difference from
    # xterm. The run is judged against the list in
    # `tests/esctest-failures.txt`, and a difference either way is what
    # fails the check.
    esctest = runInSandbox {
      name = "pymux-esctest";
      inputs = [ esctest2 ];
      env = {
        inherit esctestInclude;
        record = esctestRecord;
      };
    } ''
      export PYMUX_ESCTEST=${esctest2}/share/esctest2
      export PYMUX_ESCTEST_INCLUDE="$esctestInclude"
      ${lib.optionalString esctestRecord ''
        # The result of the build is the new list.
        export PYMUX_ESCTEST_RECORD="$out"
      ''}
      python tests/drive_with_esctest.py
    '';
  };
in
package
