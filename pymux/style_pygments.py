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

from pymux.style import a_theme, derive

__all__ = ["a_pygments_theme", "the_names"]


def the_names() -> list[str]:
    """
    Every style pygments offers, by the name
    `set-option theme pygments:<name>` takes.
    """
    from pygments.styles import get_all_styles

    return sorted(get_all_styles())


def a_pygments_theme(name: str) -> BaseStyle:
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
    except ClassNotFound as a_missing_name:
        raise KeyError(name) from a_missing_name

    return a_theme(derive(_roles(style_cls)))


a_pygments_theme = lru_cache(maxsize=None)(a_pygments_theme)


def _roles(style_cls) -> dict[str, str]:
    """
    The roles of pymux, from the colours of one pygments style.

    Everything the style did not say is a blend of what it did.
    """
    from pygments.token import Token

    def a_color(token, fallback=None, accept_bg=False):
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
    text = a_color(Token) or a_color(Token.Text) or _readable(surface)
    muted = a_color(Token.Comment, fallback=_blend(text, surface, 0.45))
    focus = a_color(Token.Keyword, fallback=_blend(text, surface, 0.25))
    focus_strong = a_color(Token.Name.Function, fallback=focus)
    error = a_color(Token.Generic.Error, fallback=None)
    if error is None or error == text:
        # The chain runs to the deleted-diff token and past it. A
        # `bg:` colour counts here, and only here: solarized hides
        # its error red in `Token.Error bg:#dc322f`, and a red slab is
        # the same signal as red text.
        error = (
            a_color(Token.Generic.Deleted, fallback=None)
            or a_color(Token.Error, fallback=None, accept_bg=True)
            or "#ff0000"
        )
    highlight = _hex(style_cls.highlight_color) if style_cls.highlight_color else _blend(
        surface, text, 0.1
    )

    def on(a):
        return _readable(a)

    return {
        "surface": surface,
        "surface-raised": _blend(surface, text, 0.07),
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


def _to_rgb(a: str) -> tuple[int, int, int]:
    return tuple(int(a[i : i + 2], 16) for i in (1, 3, 5))


def _from_rgb(rgb) -> str:
    return "#%02x%02x%02x" % rgb


def _blend(a: str, b: str, towards_b: float) -> str:
    "The colour `towards_b` of the way from `a` to `b`."
    return _from_rgb(
        tuple(
            round(r + (s - r) * towards_b)
            for r, s in zip(_to_rgb(a), _to_rgb(b))
        )
    )


def _lightness(a: str) -> float:
    r, g, b = _to_rgb(a)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _readable(a: str) -> str:
    """
    The text that reads on `a`: black or white, by contrast.

    The contrast of WCAG, not the lightness alone: a pale green is
    light enough to mistake for white by one measure and is still
    nearly four times closer to black than to white.
    """
    def the_contrast(other):
        base = _lightness(other) / 255.0
        base = base / 12.92 if base <= 0.04045 else ((base + 0.055) / 1.055) ** 2.4
        over = _lightness(a) / 255.0
        over = over / 12.92 if over <= 0.04045 else ((over + 0.055) / 1.055) ** 2.4
        return (max(base, over) + 0.05) / (min(base, over) + 0.05)

    return "#000000" if the_contrast("#000000") >= the_contrast("#ffffff") else "#ffffff"


def _other_of(a: str) -> str:
    "The text that does not read on `a`."
    return "#ffffff" if _readable(a) == "#000000" else "#000000"
