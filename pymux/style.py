"""
The colour schemes, and the one a session draws with.

`set-option theme <name>` chooses one. `THEMES` holds them by name, and
`create_theme` is how one is written: every theme needs
`Priority.MOST_PRECISE`, because these rules overlap on purpose and the
most precise one has to win.

**A theme is its roles.** The class rules below name a role each, and
the roles are what a scheme really chooses: one colour per meaning, in
`ROLES`. A theme is written as the roles it differs in, and the rules
are derived from them, so a new theme cannot forget a rule or leave a
loud colour behind: it names colours, and every rule follows.
Lillecarl/pymux#194.

A role that stays outside `derive` is a warning, not chrome: the
copy-mode cursor keeps its ANSI colours in every theme, because a
person looking for their place in a scrollback does not want it quiet.
"""

from prompt_toolkit.styles import BaseStyle, Priority, Style

__all__ = ["DEFAULT_THEME", "THEMES"]


def create_theme(rules: dict[str, str]) -> Style:
    """
    One colour scheme.

    `MOST_PRECISE` is the whole reason this exists. The rules of a
    theme overlap: `terminal titlebar` and `terminal.focused titlebar`
    both match a focused title bar, and the second one has to win. A
    theme that forgot the priority would draw the first.
    """
    return Style.from_dict(rules, priority=Priority.MOST_PRECISE)


def derive(r: dict[str, str]) -> dict[str, str]:
    """
    The class rules of a scheme, from its roles.

    `r` maps a role to a colour or style fragment. Every key below is
    one of the things pymux draws, and the roles it takes are what a
    theme chooses about it.
    """
    return {
        "border": r["border"],
        # The border of the focused pane. In the green theme it is the
        # green of the status bar; in the grey one it is the lighter
        # grey of the focused pane's name. A theme pairs these as it
        # likes, so the border has a role of its own.
        "terminal.focused border": "%s bold" % (r["focus-border"],),
        "terminal titlebar": "bg:%s %s" % (r["border"], r["text-bright"]),
        "terminal.focused titlebar": "bg:%s %s" % (r["focus"], r["text-bright"]),
        "terminal.focused titlebar name": "bg:%s %s"
        % (r["focus-strong"], r["text-bright"]),
        "terminal.focused titlebar paneindex": "bg:%s" % (r["alarm"],),
        # The names of the panes on either side, which a title bar carries
        # so that a strip can be navigated. They are not this pane, so they
        # are dimmer than its own title. Lillecarl/pymux#207.
        "titlebar neighbour": r["soft"],
        "commandline": "bg:%s %s" % (r["command"], r["text-bright"]),
        "commandline.command": "bold",
        "commandline.prompt": "bold",
        # The fill of the layout behind the panes, which shows where
        # nothing else draws.
        "background": r["border"],
        "statusbar": "noreverse bg:%s %s" % (r["signal"], r["signal-text"]),
        "statusbar window": r["text-bright"],
        "statusbar window.current": "bg:%s %s"
        % (r["signal-bright"], r["signal-text"]),
        "auto-suggestion": "bg:%s %s" % (r["suggestion"], r["suggestion-text"]),
        "message": "bg:%s %s" % (r["notice"], r["notice-text"]),
        # The part of a pane that runs off the edge of the view. A strip
        # is a row that may be wider than the screen, so a column can be
        # cut, and nothing else on the screen says so. Carl: "the
        # rightmost visible column [should have] some slightly tinted
        # background to indicate that it's cut-off."
        # Lillecarl/pymux#222.
        #
        # **One colour, and it should be derived.** A tint over a
        # background pymux did not choose is a guess: this lifts a dark
        # terminal and darkens a light one by the same amount, which is
        # right on one of them. The client knows the real background now
        # (`OSC 11`, Lillecarl/pymux#223); deriving the tint from it is
        # the second half of that issue.
        # The pane painted with the theme's own background, which
        # `paint-screen` turns on. The pane's container wears the
        # class, and prompt-toolkit draws it behind every cell the
        # program left at a default background -- the same mechanism
        # that once drew a slab of chrome behind an overlay pane's
        # output, wanted this time. A program that names its own
        # colours everywhere never sees it. Lillecarl/pymux#273.
        "painted": "bg:%s" % (r["pane"],),
        "cut": "bg:%s" % (r["cut"],),
        "clock": "bg:%s" % (r["warn-bright"],),
        "panenumber": "bg:%s" % (r["border"],),
        "panenumber focused": "bg:%s" % (r["warn"],),
        "terminated": "bg:%s %s" % (r["danger"], r["text-bright"]),
        "confirmationtoolbar": "bg:%s %s" % (r["danger-strong"], r["text-bright"]),
        "confirmationtoolbar question": "",
        "confirmationtoolbar yesno": "bg:%s" % (r["danger-deep"],),
        # The position in copy mode keeps its ANSI colours in every
        # theme. It is the one thing a person hunts for on a busy
        # screen, and a theme that could quiet it would.
        "copy-mode-cursor-position": "bg:ansiyellow ansiblack",
        "search-toolbar.prompt": "bg:%s %s" % (r["search"], r["search-prompt-text"]),
        "search-toolbar.text": "bg:%s %s" % (r["search"], r["text-dark"]),
        "search-match": "%s bg:%s" % (r["text-dark"], r["search-match"]),
        "search-match.current": "%s bg:%s underline"
        % (r["search-match-current-text"], r["search-match-current"]),
        # The completions. prompt_toolkit draws them on light grey,
        # which is a slab of daylight in the middle of a dark screen
        # once a box holds a whole screenful of them. They take the
        # colours of the box instead, and the row a person is on takes
        # the colour of its title bar.
        "completion-menu": "bg:%s %s" % (r["surface"], r["text"]),
        "completion-menu.completion": "bg:%s %s" % (r["surface"], r["text"]),
        "completion-menu.completion.current": "bg:%s %s"
        % (r["accent"], r["text-bright"]),
        "completion-menu.meta.completion": "bg:%s %s"
        % (r["surface-raised"], r["text-muted"]),
        "completion-menu.meta.completion.current": "bg:%s %s"
        % (r["accent"], r["text-bright"]),
        "scrollbar.background": "bg:%s" % (r["surface-raised"],),
        "scrollbar.button": "bg:%s" % (r["accent"],),
        # The ":" command line as a box in the middle of the screen.
        # It borrows the shape of the overlay pane below: a title row
        # over a body. Lillecarl/pymux#158.
        "commandpalette": "bg:%s" % (r["surface"],),
        "commandpalette.titlebar": "bg:%s %s" % (r["accent"], r["text-bright"]),
        # The background is named again here. A fragment style is more
        # precise than the style of the window it is drawn in, so
        # `bold` alone left the title on the background of the box and
        # cut a dark hole in the middle of the title bar.
        "commandpalette.title": "bold bg:%s %s" % (r["accent"], r["text-bright"]),
        # The overlay pane, which floats over the layout. Its body draws
        # like any pane: a cell the program left at the default background
        # shows the terminal's own background, and a rule that named a
        # colour here put a slab of chrome behind the program's output.
        # The title bar above it is chrome, and keeps its colours.
        "overlay": "",
        "overlay.titlebar": "bg:%s %s" % (r["accent"], r["text-bright"]),
        "overlay.title": "bold",
        # Pop-up dialog. Ignore built-in style.
        "dialog": "noinherit",
        "dialog.body": "noinherit",
        "dialog frame": "noinherit",
        "dialog.body text-area": "noinherit",
        "dialog.body text-area last-line": "noinherit",
    }


#: What each role draws. A theme names the roles it differs in; the
#: ones `derive` reads and a theme left out are what it keeps of the
#: default.
ROLES = {
    # The boxes that hold chrome: the completion menu, the command
    # palette. `surface-raised` is the same box, one shade up, for the
    # rows that carry a description and the scrollbar trough.
    "surface": "#1c1c1c",
    "surface-raised": "#262626",
    # Text on the boxes, and on the coloured bars.
    "text": "#d0d0d0",
    "text-bright": "#ffffff",
    "text-dark": "#000000",
    "text-muted": "#a8a8a8",
    # The names of the neighbouring panes in a title bar.
    "soft": "#dddddd",
    # An unfocused pane's border, title bar and number, and the fill of
    # the layout behind the panes.
    "border": "#888888",
    # The background a pane draws behind its cells when `paint-screen`
    # is on: the scheme's own colour, so the terminal's background is
    # never seen and a theme is the colour of the whole screen.
    # Lillecarl/pymux#273.
    "pane": "#000000",
    # A focused pane's title bar, and its name inside it. `alarm` is
    # the pane index, the loudest thing on the screen.
    "focus": "#448844",
    "focus-strong": "#88aa44",
    # The border of the focused pane, which the green theme takes from
    # the status bar and the grey one from the pane's own name.
    "focus-border": "ansigreen",
    "alarm": "#ff0000",
    # The status bar, and the current window in it. `signal-text` is
    # what reads well on the bar.
    "signal": "ansigreen",
    "signal-bright": "#44ff44",
    "signal-text": "#000000",
    # The bar a person types a command on, and the suggestion behind
    # what they type.
    "command": "#4e4e4e",
    "suggestion": "#4e5e4e",
    "suggestion-text": "#88aa88",
    # The message bar, which says what a command did.
    "notice": "#bbee88",
    "notice-text": "#222222",
    # The column a strip cuts off, and the clock and pane number, which
    # are yellow here and quiet in a quiet theme.
    "cut": "#303030",
    "warn": "#aa8800",
    "warn-bright": "#88aa00",
    # A pane that has ended, and the confirmation that asks about it.
    "danger": "#aa0000",
    "danger-strong": "#880000",
    "danger-deep": "#440000",
    # The search toolbar, and the matches it found.
    "search": "#88ff44",
    "search-prompt-text": "#444444",
    "search-match": "#88aa88",
    "search-match-current": "#aaffaa",
    "search-match-current-text": "#000000",
    # The hue of the pop-ups: the row a person is on in the completion
    # menu, the scrollbar's button, and the title bars of the command
    # palette and the overlay pane.
    "accent": "#5f5f87",
}

#: The sixteen the pane answers a program with, by the number "OSC 4"
#: asks in. These are the conventional colours of a 256 colour
#: terminal -- the same ones `pyte.colors` reports -- and they are
#: roles so that a theme can own them: with `paint-screen` on, the
#: theme colours the whole screen, and a program that asks what red
#: is hears the theme's red, not the terminal's. Lillecarl/pymux#283.
ANSI_ROLES = {
    "color-0": "#000000",
    "color-1": "#cd0000",
    "color-2": "#00cd00",
    "color-3": "#cdcd00",
    "color-4": "#0000ee",
    "color-5": "#cd00cd",
    "color-6": "#00cdcd",
    "color-7": "#e5e5e5",
    "color-8": "#7f7f7f",
    "color-9": "#ff0000",
    "color-10": "#00ff00",
    "color-11": "#ffff00",
    "color-12": "#5c5cff",
    "color-13": "#ff00ff",
    "color-14": "#00ffff",
    "color-15": "#ffffff",
}

#: The roles of a theme, with the palette underneath. `derive` reads
#: the chrome roles and ignores these; the pane's colour base is what
#: reads them.
ROLES = {**ANSI_ROLES, **ROLES}

#: The green scheme pymux has always drawn.
DEFAULT_ROLES = dict(ROLES)
DEFAULT = create_theme(derive(DEFAULT_ROLES))

#: The same scheme with the loud colours taken out, in the blue grey
#: the command palette and the completion menu already use.
#:
#: It is here so that a person who does not want the green has
#: somewhere to go, and so that choosing a theme is proven by
#: something other than the theme it starts on.
#:
#: **A picture is what says this is finished.** The green was the
#: obvious half, and a photograph of it
#: (`checks.pymux-chrome-pictures`) showed the pane index of the
#: focused pane still on pure red and its number on orange: the two
#: loudest things on the screen, in a scheme whose whole point is that
#: it is quiet. Neither is green, so neither was on the first list.
#: Lillecarl/pymux#161.
#:
#: What stays coloured is what carries a meaning rather than a mood: a
#: pane that has ended is still on red, a confirmation is still on
#: dark red, and the position in copy mode is still yellow. A theme
#: chooses its chrome, not its warnings.
GREY_ROLES = {
    **ROLES,
    "focus": "#5f5f87",
    "focus-strong": "#8787af",
    "focus-border": "#8787af",
    "alarm": "#8787af",
    "signal": "#5f5f87",
    "signal-bright": "#8787af",
    "signal-text": "#ffffff",
    "suggestion": "#4e4e5e",
    "suggestion-text": "#8888aa",
    "notice": "#8787af",
    "notice-text": "#ffffff",
    "warn": "#5f5f87",
    "warn-bright": "#5f5f87",
    "search": "#8787af",
    "search-prompt-text": "#ffffff",
    "search-match": "#8888aa",
    "search-match-current": "#5f5f87",
    "search-match-current-text": "#ffffff",
}

GREY = create_theme(derive(GREY_ROLES))

#: Every theme, by the name `set-option theme` takes.
THEMES: dict[str, BaseStyle] = {
    "default": DEFAULT,
    "grey": GREY,
}

#: The roles of each, by the same name. The scheme is derived from
#: these, and the pane's colour base is read straight off them, so a
#: theme that names a colour once has named it everywhere.
THEME_ROLES: dict[str, dict[str, str]] = {
    "default": DEFAULT_ROLES,
    "grey": GREY_ROLES,
}

#: The one a session starts on.
DEFAULT_THEME = "default"


def theme(name: str) -> BaseStyle:
    """
    The scheme of one theme name, from whatever source it names.

    `pygments:<name>` names one of the styles pygments carries, and
    anything a package installs beside them; anything else names one
    of `THEMES`. A name no source offers raises `KeyError`.
    """
    source, _, rest = name.partition(":")
    if source == "pygments":
        from pymux.style_pygments import pygments_theme

        return pygments_theme(rest)
    if source == "base16":
        from pymux.style_base16 import base16_theme

        return base16_theme(rest)
    return THEMES[name]


def _blend(a: str, b: str, towards_b: float) -> str:
    "The colour `towards_b` of the way from `a` to `b`."
    return _from_rgb(
        tuple(
            round(r + (s - r) * towards_b)
            for r, s in zip(_to_rgb(a), _to_rgb(b))
        )
    )


def _to_rgb(a: str) -> tuple[int, int, int]:
    return tuple(int(a[i : i + 2], 16) for i in (1, 3, 5))


def _from_rgb(rgb) -> str:
    return "#%02x%02x%02x" % rgb


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
    def contrast(other):
        base = _lightness(other) / 255.0
        base = base / 12.92 if base <= 0.04045 else ((base + 0.055) / 1.055) ** 2.4
        over = _lightness(a) / 255.0
        over = over / 12.92 if over <= 0.04045 else ((over + 0.055) / 1.055) ** 2.4
        return (max(base, over) + 0.05) / (min(base, over) + 0.05)

    return "#000000" if contrast("#000000") >= contrast("#ffffff") else "#ffffff"


def _other_of(a: str) -> str:
    "The text that does not read on `a`."
    return "#ffffff" if _readable(a) == "#000000" else "#000000"


def roles_of_palette(sixteen: list[str]) -> dict[str, str]:
    """
    The roles of a theme, from the sixteen colours a palette holds.

    `sixteen` is in the order a terminal numbers them, and the mapping
    is the base16 spec's: base00 is the background, base03 the muted
    tone, base05 the text, base07 the bright one, base08 the red and
    so on to the eight accents. The chrome derives from those anchors,
    the way every blend in every source derives from two colours; the
    palette itself goes through verbatim, so what a pane answers a
    program with is exactly what the scheme said. Lillecarl/pymux#282,
    Lillecarl/pymux#283.

    A scheme that put nothing sensible in a slot gets a theme that
    looks wrong there, which is honest: nothing here invents a colour
    the scheme did not name.
    """
    black, red, green, yellow, blue, _, _, white = sixteen[:8]
    muted = sixteen[8]
    bright_red = sixteen[9]
    bright_green = sixteen[10]
    bright_yellow = sixteen[11]
    bright_blue = sixteen[12]
    bright_magenta = sixteen[13]
    bright_cyan = sixteen[14]
    bright_white = sixteen[15]

    return {
        # The screen the chrome draws on, and the text on it.
        "pane": black,
        "surface": black,
        "surface-raised": _blend(black, white, 0.07),
        "text": white,
        "text-bright": bright_white,
        "text-dark": _other_of(black),
        "text-muted": muted,
        "soft": _blend(white, black, 0.15),
        "border": _blend(white, black, 0.45),
        # A focused pane's bar. The green of the default theme is the
        # signal too, and the base16 spec's green is base0B.
        "focus": green,
        "focus-strong": _blend(green, white, 0.25),
        "focus-border": green,
        "alarm": red,
        # The status bar, on the scheme's green.
        "signal": green,
        "signal-bright": _blend(green, white, 0.25),
        "signal-text": _readable(green),
        "command": _blend(black, white, 0.2),
        "suggestion": _blend(black, white, 0.07),
        "suggestion-text": _blend(white, black, 0.25),
        "notice": bright_green,
        "notice-text": _readable(bright_green),
        "cut": _blend(black, white, 0.08),
        "warn": yellow,
        "warn-bright": bright_yellow,
        # A pane that has ended, and the confirmation that asks.
        "danger": red,
        "danger-strong": _blend(red, "#000000", 0.25),
        "danger-deep": _blend(red, "#000000", 0.6),
        # The search toolbar, and the matches on the base16 spec's
        # selection background.
        "search": bright_green,
        "search-prompt-text": _readable(bright_green),
        "search-match": muted,
        "search-match-current": blue,
        "search-match-current-text": _readable(blue),
        # The hue of the pop-ups: the scheme's blue.
        "accent": blue,
        # What a pane answers a program with: the sixteen verbatim,
        # in the order the terminal numbers them, which is what
        # arrived.
        **{"color-%i" % index: colour for index, colour in enumerate(sixteen)},
    }


def roles_of(name: str) -> dict[str, str]:
    """
    The roles of one theme name, from whatever source it names.

    The same names `theme` takes, and the same `KeyError` for one it
    does not. The roles are what a theme is written as, and the
    scheme is only one of the things derived from them.
    """
    source, _, rest = name.partition(":")
    if source == "pygments":
        from pymux.style_pygments import pygments_roles

        return pygments_roles(rest)
    if source == "base16":
        from pymux.style_base16 import base16_roles

        return base16_roles(rest)
    return THEME_ROLES[name]
