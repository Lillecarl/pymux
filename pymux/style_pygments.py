"""
The themes of pygments, as pymux themes.

`set-option theme pygments:<name>` chooses one. Pygments ships
forty-nine styles - dracula, nord, gruvbox, one-dark - and any
package that follows the same interface can join them, which is how a
theme of `catppuccin[pygments]` gets here without pymux carrying it.
Lillecarl/pymux#194, Lillecarl/pymux#195.

**A pygments style offers eight colours; pymux draws thirty.** It
reliably gives a background, a foreground, a muted tone (`Comment`),
an accent or two (`Keyword`, `Name.Function`), a selection
(`highlight_color`), and an error that is not always filled in: the
authors of dracula left `Generic.Error` at the plain foreground, and a
reader who trusted it would draw a pane that ended as if nothing had
happened. #194 measured that gap across four styles.

So the rest is derived, by three rules:

- **The text on a colour is picked by its contrast.** `readable` puts
  black or white behind a background, whichever reads; this is what
  keeps a light scheme's bars dark-texted and a dark scheme's light.
- **A shade is a blend.** The raised surface, the quiet border, the
  cut-column tint are the scheme's own two greys mixed a step apart.
- **What a style left out is not invented.** An error that equals the
  plain text falls back through the deleted-diff token to a loud red,
  because "a pane ended" must never be quiet.
"""

from functools import lru_cache

from prompt_toolkit.styles import BaseStyle
from prompt_toolkit.styles.named_colors import NAMED_COLORS

from pymux.style import (
    ANSI_ROLES,
    _blend,
    _other_of,
    _readable,
    create_theme,
    derive,
)

__all__ = ["pygments_roles", "pygments_theme", "names"]


def names() -> list[str]:
    """
    Every style pygments offers, by the name
    `set-option theme pygments:<name>` takes.
    """
    from pygments.styles import get_all_styles

    return sorted(get_all_styles())


def pygments_theme(name: str) -> BaseStyle:
    """
    The pymux theme of one pygments style.

    A name nobody offers raises `KeyError`; the option turns that into
    an error a person can read. Cached, because a client reads its
    style on every render, and a style is built once.
    """
    from pygments.styles import get_style_by_name
    from pygments.util import ClassNotFound

    try:
        style_cls = get_style_by_name(name)
    except ClassNotFound as missing_name:
        raise KeyError(name) from missing_name

    return create_theme(derive(_roles(style_cls)))


pygments_theme = lru_cache(maxsize=None)(pygments_theme)


def pygments_roles(name: str) -> dict[str, str]:
    """
    The roles of one pygments style, the same ones `derive` consumes.

    `set-option theme pygments:<name>` derives its scheme from these,
    and the pane's colour base reads the same roles. A name nobody
    offers raises `KeyError`. Cached, for the same reason the scheme
    is.
    """
    from pygments.styles import get_style_by_name
    from pygments.util import ClassNotFound

    try:
        style_cls = get_style_by_name(name)
    except ClassNotFound as missing_name:
        raise KeyError(name) from missing_name

    return _roles(style_cls)


pygments_roles = lru_cache(maxsize=None)(pygments_roles)


def _roles(style_cls) -> dict[str, str]:
    """
    The roles of pymux, from the colours of one pygments style.

    Everything the style did not say is a blend of what it did.
    """
    from pygments.token import Token

    def color(token, fallback=None, accept_bg=False):
        """
        The colour in the fragment of one token, ignoring its weight.

        A `bg:` word is a background, and is skipped unless this is
        the one place one is wanted.
        """
        fragment = style_cls.styles.get(token, "")
        for word in fragment.split():
            if word.startswith("bg:"):
                if not accept_bg:
                    continue
                word = word[3:]
            elif word in _WEIGHTS:
                continue
            if word.startswith("#"):
                return _hex(word)
            if word in NAMED_COLORS:
                return NAMED_COLORS[word]
        return fallback

    surface = _hex(style_cls.background_color) if style_cls.background_color else "#000000"
    text = color(Token) or color(Token.Text) or _readable(surface)
    muted = color(Token.Comment, fallback=_blend(text, surface, 0.45))
    focus = color(Token.Keyword, fallback=_blend(text, surface, 0.25))
    focus_strong = color(Token.Name.Function, fallback=focus)
    error = color(Token.Generic.Error, fallback=None)
    if error is None or error == text:
        # The chain runs to the deleted-diff token and past it. A
        # `bg:` colour counts here, and only here: solarized hides
        # its error red in `Token.Error bg:#dc322f`, and a red slab is
        # the same signal as red text.
        error = (
            color(Token.Generic.Deleted, fallback=None)
            or color(Token.Error, fallback=None, accept_bg=True)
            or "#ff0000"
        )
    highlight = _hex(style_cls.highlight_color) if style_cls.highlight_color else _blend(
        surface, text, 0.1
    )

    def on(a):
        return _readable(a)

    def base(*tokens, conventional_key):
        """
        One colour of the pane's palette, from the first token that
        says one.

        What the style left out is the conventional colour, which is
        what every terminal paints and what the theme's own roles
        start from. Lillecarl/pymux#283.
        """
        for token in tokens:
            found = color(token)
            if found is not None:
                return found
        return ANSI_ROLES[conventional_key]

    # The palette the pane answers a program with. The brights are the
    # bases a step towards the text, which is how the schemes that
    # number brights at all draw them.
    dim = {
        "color-0": _blend(surface, "#000000", 0.35),
        "color-1": error,
        "color-2": base(Token.String, conventional_key="color-2"),
        "color-3": base(
            Token.Literal.String.Escape, Token.Number, conventional_key="color-3"
        ),
        "color-4": base(Token.Name.Builtin, Token.Name.Tag, conventional_key="color-4"),
        "color-5": base(
            Token.Keyword.Type,
            Token.Keyword.Constant,
            Token.Keyword,
            conventional_key="color-5",
        ),
        "color-6": base(
            Token.Name.Decorator,
            Token.Name.Class,
            Token.Comment.Preproc,
            conventional_key="color-6",
        ),
        "color-7": text,
        "color-8": muted,
    }

    return {
        **dim,
        "color-9": _blend(dim["color-1"], text, 0.3),
        "color-10": _blend(dim["color-2"], text, 0.3),
        "color-11": _blend(dim["color-3"], text, 0.3),
        "color-12": _blend(dim["color-4"], text, 0.3),
        "color-13": _blend(dim["color-5"], text, 0.3),
        "color-14": _blend(dim["color-6"], text, 0.3),
        "color-15": _readable(surface),
        "surface": surface,
        "surface-raised": _blend(surface, text, 0.07),
        # The scheme's own background is the pane's: with
        # `paint-screen` on, a pygments scheme colours the whole
        # screen, not only the bars. Lillecarl/pymux#273.
        "pane": surface,
        "text": text,
        "text-bright": _readable(surface),
        "text-dark": _other_of(surface),
        "text-muted": _blend(text, surface, 0.25),
        "soft": _blend(text, surface, 0.15),
        "border": _blend(text, surface, 0.45),
        "focus": focus,
        "focus-strong": focus_strong,
        "focus-border": focus,
        "alarm": error,
        "signal": focus,
        "signal-bright": _blend(focus, text, 0.25),
        "signal-text": on(focus),
        "command": _blend(surface, text, 0.2),
        "suggestion": _blend(surface, text, 0.07),
        "suggestion-text": _blend(text, surface, 0.25),
        "notice": focus_strong,
        "notice-text": on(focus_strong),
        "cut": _blend(surface, text, 0.08),
        "warn": _blend(focus, text, 0.35),
        "warn-bright": _blend(focus, text, 0.5),
        "danger": error,
        "danger-strong": _blend(error, "#000000", 0.25),
        "danger-deep": _blend(error, "#000000", 0.6),
        "search": focus_strong,
        "search-prompt-text": on(focus_strong),
        "search-match": highlight,
        "search-match-current": _blend(highlight, text, 0.3),
        "search-match-current-text": on(_blend(highlight, text, 0.3)),
        "accent": focus,
    }


#: The words of a pygments fragment that say how a colour is drawn
#: rather than what it is.
_WEIGHTS = frozenset(
    {"bold", "italic", "underline", "noinherit", "reverse", "blink", "strike"}
)


def _hex(word: str) -> str:
    "A colour as pymux names it: #rrggbb."
    word = word.lstrip("#")
    if len(word) == 3:
        word = "".join(twice for twice in (word[i] * 2 for i in range(3)))
    return "#%s" % (word.lower(),)
