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
  # The fallbacks for every glyph the font lacks, always handed over:
  # GNU Unifont, whose two files cover the whole of Unicode once each,
  # the multilingual plane and the planes above it. Its other files are
  # variants of the same glyphs and stay out.
  unifont,
}:
(mkProject {
  root = ./.;
  inherit python;
  extra = rendered: {
    postInstall = ''
      faces=${nerdFont}/share/fonts/truetype/NerdFonts/JetBrainsMono/JetBrainsMonoNerdFontMono
      fallbacks=${unifont}/share/fonts/opentype/unifont
      for file in "$faces"-{Regular,Bold,Italic,BoldItalic}.ttf "$fallbacks"/unifont{,_upper}.otf; do
        test -f "$file"
      done
      printf '{"faces": ["%s", "%s", "%s", "%s"], "fallbacks": ["%s", "%s"]}\n' \
        "$faces"-{Regular,Bold,Italic,BoldItalic}.ttf "$fallbacks"/unifont{,_upper}.otf \
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
