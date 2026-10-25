# The MCP server of pymux, as a package of its own.
#
# It is a project inside pymux, as libpymux is, and `pyproject.toml`
# beside this says why. There are no checks here: the suites that judge
# it are pymux's, because one of them drives a server that is really
# there.
{
  lib,
  python,
  stdenv,
  pyprojectHook,
  resolveBuildSystem,
  mkProject,
}:
(mkProject {
  root = ./.;
  inherit python;
  extra = rendered: {
    meta = rendered.meta // {
      description = "Drive a pymux server from an agent, over MCP";
      homepage = "https://github.com/Lillecarl/pymux";
      license = lib.licenses.bsd3;
      mainProgram = "pymux-mcp";
      platforms = lib.platforms.unix;
    };
  };
})
  {
    inherit stdenv pyprojectHook resolveBuildSystem;
  }
