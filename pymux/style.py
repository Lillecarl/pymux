"""
The colour schemes, and the one a session draws with.

`set-option theme <name>` chooses one. `THEMES` holds them by name, and
`a_theme` is how one is written: every theme needs
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


def a_theme(rules: dict[str, str]) -> Style:
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

#: The green scheme pymux has always drawn.
DEFAULT = a_theme(derive(ROLES))

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
GREY = a_theme(
    derive(
        {
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
    )
)

#: Every theme, by the name `set-option theme` takes.
THEMES: dict[str, BaseStyle] = {
    "default": DEFAULT,
    "grey": GREY,
}

#: The one a session starts on.
DEFAULT_THEME = "default"
