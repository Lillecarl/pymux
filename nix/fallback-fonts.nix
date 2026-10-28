# The font files a picture of a pane falls back to, in order, for each
# glyph its face lacks: `capture-pane --png --fallback`, as pymux-mcp
# hands it and as the gallery draws.
#
# The icons first, where programs put them: starship, yazi, eza and lsd
# write Nerd Font code points, and the symbols-only font holds Font
# Awesome, Devicons, Octicons, Codicons, Material Design, Weather Icons
# and Powerline there. Then Noto Emoji in one colour: Pillow sizes no
# colour emoji font to a cell. Then Font Awesome at its own code points,
# after the emoji because it maps some emoji too, and draws them as its
# icons. Unifont last, which covers the whole of Unicode once, in its two
# files; its other files are variants of the same glyphs.
{
  nerd-fonts,
  font-awesome,
  noto-fonts-monochrome-emoji,
  unifont,
}:
[
  "${nerd-fonts.symbols-only}/share/fonts/truetype/NerdFonts/Symbols/SymbolsNerdFontMono-Regular.ttf"
  "${noto-fonts-monochrome-emoji}/share/fonts/noto/NotoEmoji.ttf"
  "${font-awesome}/share/fonts/opentype/Font Awesome 7 Free-Solid-900.otf"
  "${font-awesome}/share/fonts/opentype/Font Awesome 7 Free-Regular-400.otf"
  "${font-awesome}/share/fonts/opentype/Font Awesome 7 Brands-Regular-400.otf"
  "${unifont}/share/fonts/opentype/unifont/unifont.otf"
  "${unifont}/share/fonts/opentype/unifont/unifont_upper.otf"
]
