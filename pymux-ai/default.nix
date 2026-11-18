# pymux's AI plugins, as a package of its own.
#
# A project inside pymux, as pymux-mcp is. pymux finds it through the
# `pymux.plugins` entry-point group, so only a pymux built with
# `pymux.withPlugins (ps: [ ps.pymux-ai ])` runs it. Its suites are its
# own, so pymux's never load a plugin that talks to a model.
{
  lib,
  python,
  stdenv,
  pyprojectHook,
  resolveBuildSystem,
  mkProject,
  mkVirtualEnv,
  callPackage,
  # The linter and formatter that the `ruff` check runs.
  ruff,
}:
let
  package =
    (mkProject {
      root = ./.;
      inherit python;
      extra = rendered: {
        passthru = rendered.passthru // { inherit checks; };
        meta = rendered.meta // {
          description = "pymux plugins that ask a model about what a pane shows";
          homepage = "https://github.com/Lillecarl/pymux";
          license = lib.licenses.bsd3;
          platforms = lib.platforms.unix;
        };
      };
    })
      {
        inherit stdenv pyprojectHook resolveBuildSystem;
      };

  testSources = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./tests
      ./pyproject.toml
      ./pymux_ai
    ];
  };

  testEnv = mkVirtualEnv "pymux-ai-test-env" { pymux-ai = [ "test" ]; };

  inherit (callPackage ../nix/suite.nix { }) suite;

  # Narrow a run while hunting:
  #
  #     PYMUX_AI_TESTS=tests/test_claude_code_encourager.py nix build --file . checks.pymux-ai-unit
  selection = builtins.getEnv "PYMUX_AI_TESTS";

  prepare = ''
    cp -r ${testSources}/tests ${testSources}/pyproject.toml .
    chmod -R +w .
    export HOME="$TMPDIR"
    export LANG=C.UTF-8
  '';

  checks = {
    unit = suite {
      name = "pymux-ai-unit";
      inputs = [ testEnv ];
      env = { inherit selection; };
      setup = prepare;
    } "timeout 300 python -m pytest $selection -q -p no:cacheprovider";

    ruff = suite {
      name = "pymux-ai-ruff";
      inputs = [ ruff ];
      # pymux's settings, which `sync-sources` copies from pyterm's
      # ruff.toml, one directory up: ruff passes over this project's
      # `pyproject.toml`, which has no `[tool.ruff]`, and finds them.
      setup = ''
        cp ${../pyproject.toml} pyproject.toml
        mkdir pymux-ai
        cd pymux-ai
      ''
      + prepare
      + ''
        cp -r ${testSources}/pymux_ai .
      '';
    } ''
      export RUFF_CACHE_DIR="$TMPDIR/ruff"
      ruff check pymux_ai tests
      ruff format --check pymux_ai tests
    '';
  };
in
package
