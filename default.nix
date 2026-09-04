{ pkgs ? import <nixpkgs> {}}:
let
  inherit (pkgs) lib;

  # Build against local working copies of ptterm and pyte when they sit
  # next to this repo, and fall back to the pinned fork commits
  # otherwise. (Neither upstream carries the kitty protocol support.)
  localSrc = path: if builtins.pathExists path then path else null;

  pttermSrc = localSrc ../ptterm;
  pyteSrc = localSrc ../pyte;
  promptToolkitSrc = localSrc ../prompt-toolkit;

  pyte = pkgs.python3Packages.callPackage ./pyte.nix {
    localSrc = pyteSrc;
  };

  prompt-toolkit = pkgs.python3Packages.callPackage ./prompt-toolkit.nix {
    localSrc = promptToolkitSrc;
  };

  ptterm = pkgs.python3Packages.callPackage ./ptterm.nix {
    inherit pyte prompt-toolkit;
    localSrc = pttermSrc;
  };

  # The terminfo entry that describes a pane, compiled from the table
  # that ptterm also answers XTGETTCAP with. A program built on
  # ncurses reads the database instead of asking, and without an entry
  # of our own it reads the one for xterm-256color and never writes a
  # curly underline.
  terminfo =
    pkgs.runCommand "pymux-terminfo"
      {
        nativeBuildInputs = [
          (pkgs.python3.withPackages (ps: [ ptterm ]))
          pkgs.ncurses
        ];
      }
      ''
        mkdir -p $out/share/terminfo
        python -m ptterm.terminfo > pymux.ti
        tic -x -o $out/share/terminfo pymux.ti

        # An entry that does not compile leaves a pane naming a
        # terminal that is not there, which is worse than naming
        # xterm.
        TERMINFO_DIRS=$out/share/terminfo: infocmp -x pymux > /dev/null
      '';

  package = pkgs.python3Packages.callPackage ./package.nix {
    inherit ptterm prompt-toolkit terminfo;
  };

  # The same two, but always from the pinned commits. A local working
  # copy that is ahead of its pin hides a pin that nobody else can
  # build; `checks.pinned` is what catches that.
  pinnedPyte = pkgs.python3Packages.callPackage ./pyte.nix { };
  pinnedPromptToolkit = pkgs.python3Packages.callPackage ./prompt-toolkit.nix { };
  pinnedPtterm = pkgs.python3Packages.callPackage ./ptterm.nix {
    pyte = pinnedPyte;
    prompt-toolkit = pinnedPromptToolkit;
  };

  pythonFor = terminal: emulator: toolkit: pkgs.python3.withPackages (ps: [
    ps.docopt-ng
    ps.hypothesis
    ps.pytest
    ps.wcwidth
    terminal
    emulator
    toolkit
  ]);

  pythonWithTests = pythonFor ptterm pyte prompt-toolkit;

  # `PYMUX_TESTS` picks what pytest runs, for instance
  # `PYMUX_TESTS=tests/test_sixel_encoder.py nix-build -A checks.pymux`.
  # It reaches the evaluation through the environment, so it only works
  # with impure evaluation, which `nix-build` uses by default.
  selection =
    let value = builtins.getEnv "PYMUX_TESTS";
    in if value == "" then "tests" else value;

  # The sources that a test run needs. Keeping them out of the store
  # copy of the whole repository keeps the build from rerunning on
  # every unrelated edit.
  testSources = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./pymux ./tests ];
  };

  # Run a test command in the build sandbox. Nothing of the run reaches
  # the machine: the sockets, the temporary directories and the
  # processes all live and die inside it.
  runInSandbox = name: command: runWith pythonWithTests name command;

  runWith = python: name: command:
    pkgs.runCommand name
      {
        nativeBuildInputs = [ python ];
        # Rerun whenever the selection changes.
        inherit selection;
      }
      ''
        cp -r ${testSources}/pymux ${testSources}/tests .
        chmod -R +w .
        export HOME="$TMPDIR"
        export TMPDIR="$TMPDIR"
        export LANG=C.UTF-8
        export PYTHONDONTWRITEBYTECODE=1

        # The entry of terminfo that a pane is told about. The wrapper
        # of the package sets this; a test runs the source, so it sets
        # it here instead.
        export PYMUX_TERMINFO=${terminfo}/share/terminfo
        ${command}
        touch "$out"
      '';

  checks = {
    # The unit tests of pymux.
    pymux = runInSandbox "pymux-tests" ''
      python -m pytest $selection -q -p no:cacheprovider
    '';

    # The end to end test. It opens a pty, starts a server and attaches
    # a client, so it needs a sandbox that gives it /dev/ptmx.
    pty = runInSandbox "pymux-pty-tests" ''
      python tests/drive_with_pty.py
    '';

    # The same tests against the pinned ptterm and pyte, so that a pin
    # which is behind the local working copy cannot pass unnoticed.
    pinned = runWith (pythonFor pinnedPtterm pinnedPyte pinnedPromptToolkit)
      "pymux-pinned-tests" ''
      python -m pytest $selection -q -p no:cacheprovider
      python tests/drive_with_pty.py
    '';
  }
  # The tests of the local working copies, when there are any.
  // lib.optionalAttrs (pttermSrc != null) {
    ptterm = runSuite "ptterm-tests" pttermSrc;
    # The hunt for deviations between ptterm and kitty. This is not a
    # gate: it finds them faster than they get fixed, and each one
    # needs a decision about whether to follow kitty or xterm.
    # `PYMUX_FUZZ` says how many examples to try.
    fuzz =
      let
        examples = let value = builtins.getEnv "PYMUX_FUZZ";
                   in if value == "" then "2000" else value;
      in
      pkgs.runCommand "ptterm-fuzz"
        {
          nativeBuildInputs = [ pythonWithTests ];
          inherit examples;
        }
        ''
          cp -r ${pttermSrc}/tests .
          chmod -R +w .
          export HOME="$TMPDIR"
          export PTTERM_KITTY=${pkgs.kitty}/lib/kitty
          export PTTERM_LIBVTERM=${pkgs.libvterm-neovim}/lib/libvterm.so
          export PTTERM_JUDGES=${judges}/bin/ptterm-judges
          export PTTERM_FUZZ="$examples"
          python -m pytest tests/fuzz_against_kitty.py -q -p no:cacheprovider
          touch "$out"
        '';
  }
  // lib.optionalAttrs (pyteSrc != null) {
    pyte = runSuite "pyte-tests" pyteSrc;
  };

  # The judges that are written in Rust: WezTerm and Alacritty, each
  # with a screen model of its own. One program answers for both, so
  # the python side asks one process instead of starting two.
  judges =
    if pttermSrc == null then null else
    pkgs.rustPlatform.buildRustPackage {
      pname = "ptterm-judges";
      version = "0.1.0";
      # Only the judges, not the whole of ptterm: otherwise every
      # change to a test rebuilds every crate.
      src = builtins.path {
        path = "${pttermSrc}/tests/judges";
        name = "ptterm-judges-source";
      };
      cargoLock = {
        lockFile = "${pttermSrc}/tests/judges/Cargo.lock";
        outputHashes = {
          "wezterm-term-0.1.0" = "sha256-Fe2rH9HegaUixPXHyHv4B8c0RI34GPbeX8mTzHCQwQ4=";
          "finl_unicode-1.3.0" = "sha256-38S6XH4hldbkb6NP+s7lXa/NR49PI0w3KYqd+jPHND0=";
        };
      };
      doCheck = false;

      # wezterm-term reads a file of termwiz by a path relative to its
      # own directory, which the vendored copy does not have: every
      # crate lands beside the others and the name carries a version.
      # The name without the version brings the path back.
      preBuild = ''
        ln -sfn "$NIX_BUILD_TOP/cargo-vendor-dir/termwiz-0.24.0" \
                "$NIX_BUILD_TOP/cargo-vendor-dir/termwiz"
      '';
    };

  # The test suite of a working copy next to this repo.
  #
  # Two emulators to compare the screen of ptterm against.
  # `PTTERM_KITTY` is the one kitty carries as a python extension, and
  # kitty is the terminal that pymux runs inside. `PTTERM_LIBVTERM` is
  # the one that Vim and Neovim carry, which leans towards xterm. Where
  # the two agree and ptterm differs, ptterm is wrong; where they
  # disagree, the difference is a choice.
  runSuite = name: source:
    pkgs.runCommand name {
      # ncurses for the check that the terminfo entry compiles.
      nativeBuildInputs = [ pythonWithTests pkgs.ncurses ];
    } ''
      cp -r ${source}/tests .
      chmod -R +w .
      export HOME="$TMPDIR"
      export PTTERM_KITTY=${pkgs.kitty}/lib/kitty
      export PTTERM_LIBVTERM=${pkgs.libvterm-neovim}/lib/libvterm.so
      export PTTERM_JUDGES=${judges}/bin/ptterm-judges

      # A comparison that cannot run proves nothing, so say so loudly
      # instead of skipping.
      python -c "import sys; sys.path.insert(0, sys.argv[1]); import kitty.fast_data_types" "$PTTERM_KITTY"
      python -c "import ctypes, os; ctypes.CDLL(os.environ['PTTERM_LIBVTERM'])"
      echo '{"data":"x","lines":1,"columns":1}' | "$PTTERM_JUDGES" > /dev/null

      python -m pytest tests -q -p no:cacheprovider
      touch "$out"
    '';

  # Development shell with the dependencies of `tests/drive_with_libtmux.py`
  # and the linters.
  shell = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (ps: [
        prompt-toolkit
        pyte
        ps.wcwidth
        ps.docopt-ng
        ps.hypothesis
        ps.libtmux
        ps.pytest
        ptterm
        ps.pyinstrument
      ]))
      pkgs.ruff
      pkgs.black
    ];
  };
in
{
  inherit package shell checks judges terminfo;
}
