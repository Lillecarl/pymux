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
  # The files for every glyph the font lacks, in order, always handed
  # over: `../nix/fallback-fonts.nix` says which and why.
  fallbackFonts,
}:
(mkProject {
  root = ./.;
  inherit python;
  extra = rendered: {
    postInstall =
      let
        fonts = {
          faces = map (
            face: "${nerdFont}/share/fonts/truetype/NerdFonts/JetBrainsMono/JetBrainsMonoNerdFontMono-${face}.ttf"
          ) [ "Regular" "Bold" "Italic" "BoldItalic" ];
          fallbacks = fallbackFonts;
        };
      in
      ''
        ${lib.concatMapStrings (file: "test -f ${lib.escapeShellArg file}\n") (fonts.faces ++ fonts.fallbacks)}
        echo ${lib.escapeShellArg (builtins.toJSON fonts)} > "$out/${python.sitePackages}/pymux_mcp/fonts.json"
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
