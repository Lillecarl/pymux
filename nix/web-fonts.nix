# The fonts `pymux web` serves: the face pymux-mcp draws pictures with,
# then `fallback-fonts.nix` in its order, as woff2 with a `fonts.css`.
# `web-fonts.py` says how. A phone has none of these, and a symbol its
# own fonts lack drew as a box. Lillecarl/pyterm#1070.
#
# Only the converted files reach the closure: `nerdFont` whole is over
# 200 MB, and four of its faces are what is drawn.
{
  runCommand,
  python3,
  # A Nerd Font package; its JetBrains Mono faces are the first family.
  nerdFont,
  # Font files, each a family of one regular face.
  fallbackFonts,
}:
let
  mono = face: "${nerdFont}/share/fonts/truetype/NerdFonts/JetBrainsMono/JetBrainsMonoNerdFontMono-${face}.ttf";
  families = [
    [
      { path = mono "Regular"; weight = "normal"; style = "normal"; }
      { path = mono "Bold"; weight = "bold"; style = "normal"; }
      { path = mono "Italic"; weight = "normal"; style = "italic"; }
      { path = mono "BoldItalic"; weight = "bold"; style = "italic"; }
    ]
  ]
  ++ map (path: [ { inherit path; weight = "normal"; style = "normal"; } ]) fallbackFonts;
in
runCommand "pymux-web-fonts"
  {
    nativeBuildInputs = [ (python3.withPackages (p: [ p.fonttools p.brotli ])) ];
    faces = builtins.toJSON families;
    passAsFile = [ "faces" ];
  }
  ''
    python3 ${./web-fonts.py} "$out" "$facesPath"
  ''
