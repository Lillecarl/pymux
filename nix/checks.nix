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
  kitty,
  mesa,
  grim,
  imagemagick,
  makeFontsConf,
  dejavu_fonts,
  perl,
  terminfo,
  testSources,
}:
let
  inherit (callPackage ./suite.nix { }) suite;

  # The conformance suite belongs to ptterm: it judges an emulator, and the
  # pane is where pymux puts one. ptterm carries the package and runs the
  # same suite on a pty of its own, so the two lists can be compared.
  inherit (ptterm) esctest2;

  # The test files of libvterm, and libvterm's own program that answers
  # them. ptterm carries both for the same reason it carries the
  # conformance suite: they judge an emulator, and a pane is where pymux
  # puts one. Here the harness is the judge and pymux is in the middle.
  inherit (ptterm) vtermSuite;

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
  # `PYMUX_TESTS=tests/test_sixel_encoder.py nix build --file . checks.pymux-unit`.
  selection =
    let
      value = builtins.getEnv "PYMUX_TESTS";
    in
    if value == "" then "tests" else value;

  # Which conformance tests run. It is a regular expression that the suite
  # matches against "Class.method", for instance
  # `PYMUX_ESCTEST_INCLUDE=BSTests nix build --file . checks.pymux-esctest`.
  # Which of libvterm's test files run, for instance
  # `PYMUX_VTERM_INCLUDE=unicode nix build --file . checks.pymux-vterm`.
  vtermInclude =
    let
      value = builtins.getEnv "PYMUX_VTERM_INCLUDE";
    in
    if value == "" then ".*" else value;

  # Write every line of the exchange between the three programs into the
  # log, for instance
  # `PYMUX_VTERM_TRACE=1 nix build --file . checks.pymux-vterm.run`.
  vtermTrace = builtins.getEnv "PYMUX_VTERM_TRACE";

  esctestInclude =
    let
      value = builtins.getEnv "PYMUX_ESCTEST_INCLUDE";
    in
    if value == "" then ".*" else value;

  # Which picture fixtures run. It is a piece of a name, for instance
  # `PYMUX_PICTURES=underlines nix build --file . checks.pymux-pictures`.
  pictureSelection = builtins.getEnv "PYMUX_PICTURES";

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
  unit = runInSandbox {
    name = "pymux-unit";
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
      # kitty is the terminal the faults get reported from, so it is
      # the one to measure. It draws with OpenGL, which llvmpipe serves
      # without a graphics card.
      kitty
      grim
      imagemagick
    ];
    env = { inherit pictureSelection; };
  } ''
    export FONTCONFIG_FILE=${fontsConf}

    # kitty draws with OpenGL and a build sandbox has no graphics card,
    # so llvmpipe draws instead. It has to be told where the driver and
    # the EGL description are: nothing here reads /run/opengl-driver.
    export LIBGL_ALWAYS_SOFTWARE=1
    export LIBGL_DRIVERS_PATH=${mesa}/lib/dri
    export __EGL_VENDOR_LIBRARY_DIRS=${mesa}/share/glvnd/egl_vendor.d
    export LD_LIBRARY_PATH=${mesa}/lib

    export PYMUX_PICTURES="$pictureSelection"
    export PYMUX_PICTURES_OUT="$out"
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
    env = { inherit esctestInclude; };
  } ''
    export PYMUX_ESCTEST=${esctest2}/share/esctest2
    export PYMUX_ESCTEST_INCLUDE="$esctestInclude"
    export PYMUX_ESCTEST_OUT="$out"
    python tests/drive_with_esctest.py
  '';

  # The test suite of libvterm, with pymux in the middle of it.
  #
  # `checks.ptterm-vterm` plugs ptterm in where libvterm stands and judges
  # our model. This judges our wire: the bytes of a test file reach a
  # program in a full screen pane, pymux renders, and a real libvterm reads
  # what pymux emitted and answers the assertions.
  #
  # So the judge is libvterm's own `t/harness`, built as it stands, and
  # nothing of ours decides anything. `tests/drive_with_vterm.py` says which
  # files can run this way and why the rest cannot.
  vterm = runInSandbox {
    name = "pymux-vterm";
    inputs = [
      perl
      vtermSuite.harness
    ];
    env = { inherit vtermInclude vtermTrace; };
  } ''
    export PYMUX_VTERM=${vtermSuite.tests}/share/libvterm-tests
    export PYMUX_VTERM_HARNESS=${vtermSuite.harness}/bin/libvterm-harness
    export PYMUX_VTERM_INCLUDE="$vtermInclude"
    export PYMUX_VTERM_TRACE="$vtermTrace"
    export PYMUX_VTERM_TMP="$TMPDIR"
    export PYMUX_VTERM_OUT="$out"
    python tests/drive_with_vterm.py
  '';
}
