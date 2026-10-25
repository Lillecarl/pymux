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
  # The font `pane.capture` hands pymux for a PNG when pymux finds none
  # of its own: a Nerd Font, for the icons a prompt or a file manager
  # draws. Its faces go in `pymux_mcp/fonts.json`, written into the
  # output, never beside the source.
  nerdFont,
}:
(mkProject {
  root = ./.;
  inherit python;
  extra = rendered: {
    postInstall = ''
      faces=${nerdFont}/share/fonts/truetype/NerdFonts/JetBrainsMono/JetBrainsMonoNerdFontMono
      for face in Regular Bold Italic BoldItalic; do
        test -f "$faces-$face.ttf"
      done
      printf '["%s", "%s", "%s", "%s"]\n' "$faces-Regular.ttf" "$faces-Bold.ttf" "$faces-Italic.ttf" "$faces-BoldItalic.ttf" \
        > "$out/${python.sitePackages}/pymux_mcp/fonts.json"
    '';
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
