# The suites that judge pymux.
#
# It declares its own inputs, so `default.nix` holds the package and does not
# carry the X server, the two terminal emulators, the compositor and the
# screenshot tools that only a test needs.
#
# `testSources` comes from `default.nix`. The package itself is not an input:
# a pymux suite runs against the source in `testSources`, where pyte and
# ptterm run their suites against the installed package.
#
# The entry of terminfo that a pane is told about needs no input either. It
# rides inside `pyte`, which is in `pythonWithTests` already.
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
  pyinstrument,
  callPackage,
  xorg-server,
  xterm,
  xdotool,
  cage,
  foot,
  kitty,
  mesa,
  vttest,
  grim,
  imagemagick,
  makeFontsConf,
  dejavu_fonts,
  perl,
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

  # The reference tests of Alacritty, and the judge that reads one. The
  # same reasoning again: Alacritty judges an emulator, ptterm carries it
  # for the panel, and a pane is where pymux puts an emulator. `judges`
  # holds the Rust crate that both the panel and that judge are built
  # from.
  inherit (ptterm) alacrittySuite judges;

  # The walker that drives vttest. vttest is interactive: it draws a screen
  # and waits for a key, so nothing outside it can tell a screen that is
  # finished from one that is still arriving. The walker can, because it
  # owns the pty vttest runs on. It belongs to ptterm, which is what it
  # models, and here it is the program that gets photographed.
  inherit (ptterm) vttestWalker;

  pythonWithTests = python.withPackages (ps: [
    ptterm
    prompt-toolkit
    docopt-ng
    hypothesis
    pytest
    wcwidth
    # The profiler. It is in every suite's python rather than in one,
    # because it is instrumentation: a person reaching for it wants it
    # where they already are, and it costs nothing until something
    # imports it.
    pyinstrument
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

  # Which of the end to end checks run, comma separated and without the
  # `check_` in front, for instance
  # `PYMUX_PTY_CHECKS=a_pane_that_changes_nothing nix build --file . checks.pymux-pty`.
  # A whole run starts seventeen servers, and hunting a check that is
  # red by luck means running that one check many times.
  ptyChecks = builtins.getEnv "PYMUX_PTY_CHECKS";

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

  # Which of Alacritty's reference tests run, for instance
  # `PYMUX_ALACRITTY_INCLUDE=vttest nix build --file . checks.pymux-alacritty`.
  alacrittyInclude =
    let
      value = builtins.getEnv "PYMUX_ALACRITTY_INCLUDE";
    in
    if value == "" then ".*" else value;

  # Write what the pane and the judge saw into the log, for instance
  # `PYMUX_ALACRITTY_TRACE=1 nix build --file . checks.pymux-alacritty.run`.
  alacrittyTrace = builtins.getEnv "PYMUX_ALACRITTY_TRACE";

  # Keep the bytes pymux emitted for each test, for instance
  # `PYMUX_ALACRITTY_WIRE=1 nix build --file . checks.pymux-alacritty.run`.
  # They land in `result/wire/<name>.bin`.
  alacrittyWire = builtins.getEnv "PYMUX_ALACRITTY_WIRE";

  esctestInclude =
    let
      value = builtins.getEnv "PYMUX_ESCTEST_INCLUDE";
    in
    if value == "" then ".*" else value;

  # Which picture fixtures run. It is a piece of a name, for instance
  # `PYMUX_PICTURES=underlines nix build --file . checks.pymux-pictures`.
  pictureSelection = builtins.getEnv "PYMUX_PICTURES";

  # Which shapes the frame measurement takes, and how far a count may
  # move from its budget, for instance
  # `PYMUX_FRAME_INCLUDE=strip nix build --file . checks.pymux-frame-instructions`.
  frameInclude = builtins.getEnv "PYMUX_FRAME_INCLUDE";
  frameTolerance = builtins.getEnv "PYMUX_FRAME_TOLERANCE";

  # How big a window the profiler draws, and how many frames of it, for
  # instance
  # `PYMUX_PROFILE_PANES=16 nix build --file . checks.pymux-profile.run`.
  profilePanes = builtins.getEnv "PYMUX_PROFILE_PANES";
  profileFrames = builtins.getEnv "PYMUX_PROFILE_FRAMES";

  # How much work the leak check does, and which recordings it feeds.
  # A leak shows at any volume, so the gate feeds a little: two rounds
  # of eight panes at 128 kB each. A few hundred megabytes is
  # `PYMUX_LEAKS_BYTES=1000000 PYMUX_LEAKS_ROUNDS=20 nix build --file . checks.pymux-leaks`.
  leaksRounds = builtins.getEnv "PYMUX_LEAKS_ROUNDS";
  leaksPanes = builtins.getEnv "PYMUX_LEAKS_PANES";
  leaksInclude = builtins.getEnv "PYMUX_LEAKS_INCLUDE";
  leaksTolerance = builtins.getEnv "PYMUX_LEAKS_TOLERANCE";
  leaksBytes = builtins.getEnv "PYMUX_LEAKS_BYTES";
  leaksTrace = builtins.getEnv "PYMUX_LEAKS_TRACE";
  leaksRoute = builtins.getEnv "PYMUX_LEAKS_ROUTE";

  # Which item of vttest's main menu gets photographed, and in which
  # terminals, for instance
  # `PYMUX_VTTEST_INCLUDE='^9 ' nix build --file . checks.pymux-vttest-pictures.run`.
  # The whole of vttest is five hundred screens and each one is
  # photographed twice, so the default is one item and one terminal.
  vttestInclude =
    let
      value = builtins.getEnv "PYMUX_VTTEST_INCLUDE";
    in
    if value == "" then "^4 " else value;

  # Which pictures of pymux's own chrome to take, and in which
  # terminals, for instance
  # `PYMUX_CHROME=palette nix build --file . checks.pymux-chrome-pictures`.
  # Every fixture in every terminal by default: there are six of them
  # and each one is a single picture, where the comparison check takes
  # two of everything.
  chromeSelection = builtins.getEnv "PYMUX_CHROME";
  chromeTerminals = builtins.getEnv "PYMUX_CHROME_TERMINALS";

  # xterm is the one, and `tests/photograph_vttest.py` says why: it is the
  # only one of the three that draws the DEC line attributes at all.
  vttestTerminals =
    let
      value = builtins.getEnv "PYMUX_VTTEST_TERMINALS";
    in
    if value == "" then "xterm" else value;

  # A terminal emulator draws with the fonts that fontconfig finds, and the
  # build sandbox has no /etc/fonts at all. Without this every terminal dies
  # at startup, or draws with whatever it falls back to, which is not the
  # same twice.
  fontsConf = makeFontsConf { fontDirectories = [ dejavu_fonts ]; };

  # What every seat needs: a display server, a terminal emulator and the
  # tools that find a window and photograph it. Two checks take pictures,
  # and both need all of it.
  seatInputs = [
    # The X seat: a server, a terminal that speaks nothing else, and the
    # tools that find a window and take its picture.
    xorg-server
    xterm
    xdotool
    # The Wayland seat: a kiosk compositor that gives its one window the
    # whole output, a terminal that speaks nothing else, and the tool that
    # takes a picture of that output.
    cage
    foot
    # kitty is the terminal the faults get reported from, so it is the one
    # to measure. It draws with OpenGL, which llvmpipe serves without a
    # graphics card.
    kitty
    grim
    imagemagick
  ];

  # kitty draws with OpenGL and a build sandbox has no graphics card, so
  # llvmpipe draws instead. It has to be told where the driver and the EGL
  # description are: nothing here reads /run/opengl-driver.
  seatSetup = ''
    export FONTCONFIG_FILE=${fontsConf}
    export LIBGL_ALWAYS_SOFTWARE=1
    export LIBGL_DRIVERS_PATH=${mesa}/lib/dri
    export __EGL_VENDOR_LIBRARY_DIRS=${mesa}/share/glvnd/egl_vendor.d
    export LD_LIBRARY_PATH=${mesa}/lib
  '';

  # Nothing of a run reaches the machine: the sockets, the temporary
  # directories and the processes all live and die inside the build sandbox.
  prepare = ''
    cp -r ${testSources}/pymux ${testSources}/libpymux ${testSources}/tests .
    chmod -R +w .
    export HOME="$TMPDIR"
    export LANG=C.UTF-8
    export PYTHONDONTWRITEBYTECODE=1
  '';

  # `inputs` adds to what a run may call, and `pythonWithTests` is in every
  # one of them. `env` names the variables that a run reads, and a change to
  # one of them rebuilds the check, which is what makes the knobs above work.
  runInSandbox =
    {
      name,
      inputs ? [ ],
      env ? { },
      setup ? "",
    }:
    command:
    suite {
      inherit name env;
      inputs = [ pythonWithTests ] ++ inputs;
      setup = prepare + setup;
    } command;
in
{
  # The unit tests of pymux.
  unit =
    runInSandbox
      {
        name = "pymux-unit";
        env = { inherit selection; };
      }
      ''
        python -m pytest $selection -q -p no:cacheprovider
      '';

  # What it costs to lay a window out and draw the frame around its
  # panes, in bytecode instructions.
  #
  # Every other check here asks whether pymux draws the right cells. A
  # change that makes a frame ten times more expensive passes all of
  # them, and nobody notices until pymux feels wrong under a hand.
  #
  # The unit is not a second. A second belongs to the machine that
  # counted it, and this sandbox runs beside other jobs. An instruction
  # count is the same on every machine and under any load, so a budget
  # file can hold it.
  #
  # `PYTHONHASHSEED` is pinned because the order of a set decides a
  # branch, and a branch decides a count.
  frame =
    runInSandbox
      {
        name = "pymux-frame-instructions";
        env = { inherit frameInclude frameTolerance; };
        setup = ''
          export PYMUX_FRAME_INCLUDE="$frameInclude"
          export PYMUX_FRAME_TOLERANCE="$frameTolerance"
          export PYMUX_FRAME_OUT="$out"
          export PYTHONHASHSEED=0
        '';
      }
      ''
        python tests/measure_a_frame.py
      '';

  # What pymux still holds after a pane, a window or a client has gone.
  #
  # A multiplexer is a program a person leaves running for weeks, so a
  # pane's worth of objects kept on every `kill-pane` is a leak nobody
  # sees until the machine swaps. Nothing else here asks the question.
  #
  # It needs no pty and no seat: the clients are in-process and the
  # bytes go into the same `Stream.feed` a pty would call.
  # `tests/what_leaks.py` says what a round does and how to read a red
  # run.
  #
  # `PYTHONHASHSEED` is pinned because the order of a set decides which
  # referrer a chain names first.
  leaks =
    runInSandbox
      {
        name = "pymux-leaks";
        env = {
          inherit
            leaksRounds
            leaksPanes
            leaksInclude
            leaksTolerance
            leaksBytes
            leaksTrace
            leaksRoute
            ;
        };
        setup = ''
          export PYMUX_LEAKS_RECORDINGS=${alacrittySuite}/share/alacritty-ref
          export PYMUX_LEAKS_ROUNDS="$leaksRounds"
          export PYMUX_LEAKS_PANES="$leaksPanes"
          export PYMUX_LEAKS_INCLUDE="$leaksInclude"
          export PYMUX_LEAKS_TOLERANCE="$leaksTolerance"
          export PYMUX_LEAKS_BYTES="$leaksBytes"
          export PYMUX_LEAKS_TRACE="$leaksTrace"
          export PYMUX_LEAKS_ROUTE="$leaksRoute"
          export PYTHONHASHSEED=0
        '';
      }
      ''
        python tests/what_leaks.py
      '';

  # Where the time of a frame goes. Not a gate, and it judges nothing:
  # a sampling profiler reports wall clock, and this sandbox runs
  # beside other jobs. `tests/profile_a_frame.py` says what it runs and
  # how to read it. It needs a pty, because it runs real panes.
  profile =
    runInSandbox
      {
        name = "pymux-profile";
        env = { inherit profilePanes profileFrames; };
        setup = ''
          export PYMUX_PROFILE_PANES="$profilePanes"
          export PYMUX_PROFILE_FRAMES="$profileFrames"
          export PYMUX_PROFILE_OUT="$out"
        '';
      }
      ''
        python tests/profile_a_frame.py
      '';

  # The end to end test. It opens a pty, starts a server and attaches a
  # client, so it needs a sandbox that gives it /dev/ptmx.
  pty =
    runInSandbox
      {
        name = "pymux-pty-tests";
        env = {
          PYMUX_PTY_CHECKS = ptyChecks;
        };
      }
      ''
        python tests/drive_with_pty.py
      '';

  # The same test, over the other route. `pymux integrated` puts the
  # server and the client in one process and carries the packets in
  # queues, so no socket is between them.
  #
  # The two runs together say which side a fault is on. A check that
  # fails here and passes above is the transport; one that fails in
  # both is the server or the client.
  integrated =
    runInSandbox
      {
        name = "pymux-integrated-tests";
        env = {
          PYMUX_PTY_CHECKS = ptyChecks;
        };
      }
      ''
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
  pictures =
    runInSandbox
      {
        name = "pymux-pictures";
        inputs = seatInputs;
        env = { inherit pictureSelection; };
      }
      (
        seatSetup
        + ''
          export PYMUX_PICTURES="$pictureSelection"
          export PYMUX_PICTURES_OUT="$out"
          python tests/take_a_picture.py
        ''
      );

  # The same picture, of vttest.
  #
  # `pictures` above photographs four fixtures that a person wrote by
  # hand. vttest holds five hundred screens, and they are the awkward
  # ones: double sized rows, national character sets, the reports of a
  # VT420. It is interactive, so a walker drives it and says when each
  # screen is finished; `ptterm/tests/drive_with_vttest.py` is that
  # walker and `tests/photograph_vttest.py` is the harness around it.
  #
  # It is not a gate. One item of the main menu, in one terminal, is
  # what it does by default, because the whole of vttest twice over is
  # twenty minutes for each terminal.
  vttestPictures =
    runInSandbox
      {
        name = "pymux-vttest-pictures";
        inputs = seatInputs ++ [ vttest ];
        env = { inherit vttestInclude vttestTerminals; };
      }
      (
        seatSetup
        + ''
          export PYMUX_VTTEST=${vttest}/bin/vttest
          export PYMUX_VTTEST_WALKER=${vttestWalker}/drive_with_vttest.py
          export PYMUX_VTTEST_INCLUDE="$vttestInclude"
          export PYMUX_VTTEST_TERMINALS="$vttestTerminals"
          export PYMUX_VTTEST_OUT="$out"
          python tests/photograph_vttest.py
        ''
      );

  # A picture of what pymux draws around a pane.
  #
  # `pictures` above writes `set full-screen on` before every fixture,
  # which is what takes pymux's own chrome off the screen. So nothing
  # has ever photographed the status line, a pane title bar or the
  # command palette, and nothing could: a headless compositor owns no
  # input device, so nothing could press a key to open any of it.
  # `tests/drive_in_a_terminal.py` is the way round, and
  # `tests/photograph_the_chrome.py` is the harness around it.
  # Lillecarl/pymux#161.
  #
  # It is not a gate, and it judges nothing. A picture of chrome has no
  # bare side to subtract, because the chrome is the thing pymux adds.
  # Judging it needs a recorded image, and recording one before
  # anybody has looked would record whatever it draws today, faults
  # and all. Reading the pictures is the work.
  chromePictures =
    runInSandbox
      {
        name = "pymux-chrome-pictures";
        inputs = seatInputs;
        env = { inherit chromeSelection chromeTerminals; };
      }
      (
        seatSetup
        + ''
          export PYMUX_CHROME="$chromeSelection"
          export PYMUX_CHROME_TERMINALS="$chromeTerminals"
          export PYMUX_CHROME_OUT="$out"
          python tests/photograph_the_chrome.py
        ''
      );

  # The conformance suite, run in a pane. It is not a pass or fail of its
  # own: most of it fails, and each failure names a real difference from
  # xterm. The run is judged against the list in
  # `tests/esctest-failures.txt`, and a difference either way is what
  # fails the check.
  esctest =
    runInSandbox
      {
        name = "pymux-esctest";
        inputs = [ esctest2 ];
        env = { inherit esctestInclude; };
      }
      ''
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
  vterm =
    runInSandbox
      {
        name = "pymux-vterm";
        inputs = [
          perl
          vtermSuite.harness
        ];
        env = { inherit vtermInclude vtermTrace; };
      }
      ''
        export PYMUX_VTERM=${vtermSuite.tests}/share/libvterm-tests
        export PYMUX_VTERM_HARNESS=${vtermSuite.harness}/bin/libvterm-harness
        export PYMUX_VTERM_INCLUDE="$vtermInclude"
        export PYMUX_VTERM_TRACE="$vtermTrace"
        export PYMUX_VTERM_TMP="$TMPDIR"
        export PYMUX_VTERM_OUT="$out"
        python tests/drive_with_vterm.py
      '';

  # The reference tests of Alacritty, with pymux in the middle of them.
  #
  # Alacritty records a real program: the bytes it wrote and the grid they
  # made. This puts those bytes on the screen of a full screen pane and
  # gives what pymux emitted to a real `Term`, which compares the grid it
  # builds against the recorded one.
  #
  # It is the second borrowed suite to go this way and the first that is
  # not libvterm's, so `tests/middleman.py` is shared and only the judging
  # differs. `tests/drive_with_alacritty.py` says which tests can run and
  # why the rest cannot.
  alacritty =
    runInSandbox
      {
        name = "pymux-alacritty";
        env = { inherit alacrittyInclude alacrittyTrace alacrittyWire; };
      }
      ''
        export PYMUX_ALACRITTY=${alacrittySuite}/share/alacritty-ref
        export PYMUX_ALACRITTY_JUDGE=${judges.rust}/bin/alacritty-ref
        export PYMUX_ALACRITTY_INCLUDE="$alacrittyInclude"
        export PYMUX_ALACRITTY_TRACE="$alacrittyTrace"
        export PYMUX_ALACRITTY_WIRE="$alacrittyWire"
        export PYMUX_ALACRITTY_TMP="$TMPDIR"
        export PYMUX_ALACRITTY_OUT="$out"
        python tests/drive_with_alacritty.py
      '';
}
