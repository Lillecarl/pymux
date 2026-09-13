# The package this repository builds. The suites that judge it live in
# `nix/checks.nix`, which declares its own inputs, so the terminal emulators,
# the display servers and the conformance suite that only a test needs are
# not named here.
#
# Nothing else belongs in this repository: the dev shell and the collection
# that assembles this with its siblings live in pyterm.
#
# **This is a pyproject.nix builders package, not a nixpkgs one.** What pymux
# needs is declared once, in `pyproject.toml`, and the renderer reads it: no
# `propagatedBuildInputs` here, and no second copy of the list in
# `nix/checks.nix`. An environment is a virtualenv rather than a PYTHONPATH,
# so there is nothing for an older copy to shadow and no module/application
# distinction to fall off. Lillecarl/pymux#319.
#
# `mkProject` and the three hooks come from the set the collection builds in
# `pyterm/nix/python-set.nix`. Everything pymux imports comes from that set
# too, by name, out of `pyproject.toml`.
#
# ptterm and pyterm-pytest arrive as arguments and come from the set, like
# everything else pymux imports. Both are here for their passthru and not for
# their modules: the conformance suites ptterm builds, and the wayland
# bindings pyterm-pytest generates. A tool is not a suite.
#
# mesa arrives as an argument as well, and only the checks use it: kitty
# draws with OpenGL and a build sandbox has no graphics card.
{
  lib,
  python,
  stdenv,
  pyprojectHook,
  resolveBuildSystem,
  mkVirtualEnv,
  mkProject,
  runCommand,
  ptterm,
  # The shared rig, whose seats the picture scripts borrow.
  pyterm-pytest,
  installShellFiles,
  callPackage,
  mesa,
  # Only the checks use these: the readers of the clipboard fence.
  wl-clipboard,
  xclip,
}:
let
  # The schemes of the base16 spec, converted to one JSON object while
  # the package is built: `set-option theme base16:<name>` reads it,
  # and a theme that needed a YAML parser to be read would be a theme
  # that could not be read anywhere. Pinned, because a scheme that
  # changes under a reader is a theme that lies. Lillecarl/pymux#282.
  base16-schemes-json = runCommand "pymux-base16-schemes.json"
    {
      base16 = builtins.fetchTarball {
        url = "https://github.com/tinted-theming/schemes/archive/fdca32a0d14ec80ad83a78a9ccb85592ca6cb9e1.tar.gz";
        sha256 = "sha256-LMHxSQJIv6QJJkO9W7sAkJujlwusFf9/Ct0SYJ6GyHQ=";
      };
      nativeBuildInputs = [ (python.withPackages (p: [ p.pyyaml ])) ];
    }
    ''
    python3 ${./nix/convert-base16.py} "$base16/base16" "$(pwd)/base16-schemes.json"
    install -Dm644 base16-schemes.json "$out"/base16-schemes.json
  '';

  package =
    (mkProject {
      root = ./.;
      inherit python;
      extra = rendered: {
        # The completion scripts of bash, zsh and fish, written from
        # argcomplete and put where a shell loads them. The script that
        # answers a Tab is generic: it makes the shell ask `pymux`, and
        # pymux answers from its parser tree. Lillecarl/pymux#48.
        #
        # The renderer's own `nativeBuildInputs` carry the hooks, so this
        # appends rather than replaces.
        nativeBuildInputs = rendered.nativeBuildInputs ++ [ installShellFiles ];

        # `render-completions.py` imports pymux, which imports
        # prompt_toolkit and ptterm. A builders package propagates nothing,
        # so the build environment has none of them: the script runs on a
        # virtualenv of what pymux declares, with the package just installed
        # in front of it. The specification is `rendered.passthru`, so this
        # is still one dependency list and not two.
        postInstall =
          let
            deps = mkVirtualEnv "pymux-completions-env" rendered.passthru.dependencies;
          in
          ''
            PYTHONPATH="$out/${python.sitePackages}" \
              ${deps}/bin/python ${./nix/render-completions.py} pymux "$PWD/rendered"
            installShellCompletion --cmd pymux \
              --bash rendered/bash \
              --zsh rendered/zsh \
              --fish rendered/fish
            install -Dm644 ${base16-schemes-json}/base16-schemes.json \
              "$out/${python.sitePackages}/pymux/base16-schemes.json"
          '';

        passthru = rendered.passthru // { inherit checks; };

        meta = rendered.meta // {
          description = "Pure Python terminal multiplexer (tmux alternative)";
          homepage = "https://github.com/prompt-toolkit/pymux";
          license = lib.licenses.bsd3;
          mainProgram = "pymux";
          platforms = lib.platforms.unix;
        };
      };
    })
      {
        inherit stdenv pyprojectHook resolveBuildSystem;
      };

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
      # pytest reads its settings from the root it finds, and a root with
      # no config file is a root with no settings. `anyio_mode` is in here,
      # so without this a coroutine test fails in the sandbox while passing
      # in a checkout. Lillecarl/pymux#87.
      ./pyproject.toml
    ];
  };

  # What every suite runs on. `test` is the extra that `pyproject.toml`
  # declares for exactly this, so the suites' dependencies are written beside
  # the package's own and a reader sees one list.
  #
  # pymux itself is in it. Under nixpkgs it could not be -- `withPackages`
  # drops an application, and with it everything the application propagates
  # -- which is why the old environment repeated pymux's dependency list by
  # hand. Lillecarl/pymux#319.
  testEnv = mkVirtualEnv "pymux-test-env" {
    pymux = [
      "test"
      "catppuccin"
    ];
  };

  # ptterm goes in for its passthru: the conformance suites that pymux runs
  # in a pane are built once, in ptterm, and a tool is not a suite.
  #
  # mesa arrives as an argument because the scope holds a python binding
  # under that name and not the one that draws. pyterm passes the right one.
  checks = callPackage ./nix/checks.nix {
    inherit
      testEnv
      testSources
      ptterm
      mesa
      wl-clipboard
      xclip
      ;
    waylandProtocols = pyterm-pytest.waylandProtocols;
    # The base16 collection the package carries, for the gallery that
    # photographs a base16 theme and the unit tests that read one.
    inherit base16-schemes-json;
  };
in
package
