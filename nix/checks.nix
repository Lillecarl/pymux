# The suites that judge pymux.
#
# It declares its own inputs, so `default.nix` holds the package and does not
# carry the X server, the two terminal emulators, the compositor and the
# screenshot tools that only a test needs.
#
# `terminfo` and `testSources` come from `default.nix`. The package itself is
# not an input: a pymux suite runs against the source in `testSources`, where
# pyte and ptterm run their suites against the installed package.
#
# `nix/suite.nix` says why a check is two derivations.
{
  lib,
  python,
  ptterm,
  prompt-toolkit,
  docopt-ng,
  pytest,
  hypothesis,
  wcwidth,
  callPackage,
  xorg-server,
  xterm,
  xdotool,
  cage,
  foot,
  grim,
  imagemagick,
  makeFontsConf,
  dejavu_fonts,
  terminfo,
  testSources,
}:
let
  inherit (callPackage ./suite.nix { }) suite;

  esctest2 = callPackage ./esctest2.nix { inherit python; };

  pythonWithTests = python.withPackages (ps: [
    ptterm
    prompt-toolkit
    docopt-ng
    hypothesis
    pytest
    wcwidth
  ]);

  # Knobs that reach the evaluation through the environment. They work
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

  # Which picture fixtures run. It is a piece of a name, for instance
  # `PYMUX_PICTURES=underlines nix build --file . checks.pictures`.
  pictureSelection = builtins.getEnv "PYMUX_PICTURES";

  # Set `PYMUX_PICTURES_KEEP` to anything and a difference between the two
  # pictures does not fail the build. Nix takes the output of a build that
  # failed away, so a run that judges leaves nothing to look at; with this,
  # the result of the build is the pictures.
  pictureKeep = builtins.getEnv "PYMUX_PICTURES_KEEP";

  # Set `PYMUX_PICTURES_RECORD` to anything and the picture check writes the
  # list of differences that stand now, instead of judging the run against
  # the recorded one. The result of the build is that list, ready to copy
  # over `tests/picture-differences.txt`.
  pictureRecord = builtins.getEnv "PYMUX_PICTURES_RECORD" != "";

  # A terminal emulator draws with the fonts that fontconfig finds, and the
  # build sandbox has no /etc/fonts at all. Without this every terminal dies
  # at startup, or draws with whatever it falls back to, which is not the
  # same twice.
  fontsConf = makeFontsConf { fontDirectories = [ dejavu_fonts ]; };

  # Nothing of a run reaches the machine: the sockets, the temporary
  # directories and the processes all live and die inside the build sandbox.
  prepare = ''
    cp -r ${testSources}/pymux ${testSources}/libpymux ${testSources}/tests .
    chmod -R +w .
    export HOME="$TMPDIR"
    export LANG=C.UTF-8
    export PYTHONDONTWRITEBYTECODE=1

    # The entry of terminfo that a pane is told about. The wrapper of the
    # package sets this; a test runs the source, so it sets it here.
    export PYMUX_TERMINFO=${terminfo}/share/terminfo
  '';

  # `inputs` adds to what a run may call, and `pythonWithTests` is in every
  # one of them. `env` names the variables that a run reads, and a change to
  # one of them rebuilds the check, which is what makes the knobs above work.
  runInSandbox =
    { name, inputs ? [ ], env ? { }, setup ? "" }:
    command:
    suite {
      inherit name env;
      inputs = [ pythonWithTests ] ++ inputs;
      setup = prepare + setup;
    } command;
in
{
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

  # The same test, over the other route. `pymux integrated` puts the
  # server and the client in one process and carries the packets in
  # queues, so no socket is between them.
  #
  # The two runs together say which side a fault is on. A check that
  # fails here and passes above is the transport; one that fails in
  # both is the server or the client.
  integrated = runInSandbox { name = "pymux-integrated-tests"; } ''
    export PYMUX_ROUTE=integrated
    python tests/drive_with_pty.py
  '';

  # The picture of a real terminal, with pymux in it and without it.
  #
  # Every other check here stops at the cell. This one runs the same
  # program twice in the same terminal emulator, on a display server of its
  # own, and subtracts one screenshot from the other. It catches what pymux
  # writes out again, which nothing else does.
  #
  # The result is a directory, so a run always leaves its pictures behind:
  # `result/<terminal>/<fixture>/{bare,pymux,difference}.png`.
  pictures = runInSandbox {
    name = "pymux-pictures";
    inputs = [
      # The X seat: a server, a terminal that speaks nothing else,
      # and the tools that find a window and take its picture.
      xorg-server
      xterm
      xdotool
      # The Wayland seat: a kiosk compositor that gives its one
      # window the whole output, a terminal that speaks nothing
      # else, and the tool that takes a picture of that output.
      cage
      foot
      grim
      imagemagick
    ];
    env = {
      inherit pictureSelection pictureKeep;
      record = pictureRecord;
    };
  } ''
    export FONTCONFIG_FILE=${fontsConf}
    export PYMUX_PICTURES="$pictureSelection"
    export PYMUX_PICTURES_KEEP="$pictureKeep"
    export PYMUX_PICTURES_OUT="$out"
    ${lib.optionalString pictureRecord ''
      # The result of the build is the new list.
      export PYMUX_PICTURES_RECORD="$out"
    ''}
    python tests/take_a_picture.py
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
}
