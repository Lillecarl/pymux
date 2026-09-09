"""
The colour schemes, and the one a session draws with.

`set-option theme <name>` chooses one. `THEMES` holds them by name, and
`a_theme` is how one is written: every theme needs
`Priority.MOST_PRECISE`, because these rules overlap on purpose and the
most precise one has to win.

A theme carries the whole scheme and not a difference from another one.
`GREY` is written as `DEFAULT` with the green rules replaced, because
that is what it is, and a reader can see the whole of what it changes.

**A theme is forty rules, and it should be ten colours.** Naming the
roles a scheme is really made of is the work, and it is
Lillecarl/pymux#194. This module is what a theme is chosen through, and
nothing here decides what a theme is made of.
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


DEFAULT_RULES = {
    "border": "#888888",
    "terminal.focused border": "ansigreen bold",
    #'terminal titleba':            'bg:#aaaaaa #dddddd ',
    "terminal titlebar": "bg:#888888 #ffffff",
    #    'terminal titlebar paneindex':  'bg:#888888 #000000',
    "terminal.focused titlebar": "bg:#448844 #ffffff",
    "terminal.focused titlebar name": "bg:#88aa44 #ffffff",
    "terminal.focused titlebar paneindex": "bg:#ff0000",
    # The names of the panes on either side, which a title bar carries
    # so that a strip can be navigated. They are not this pane, so they
    # are dimmer than its own title. Lillecarl/pymux#207.
    "titlebar neighbour": "#dddddd",
    #    'titlebar title':               '',
    #    'titlebar name':                '#ffffff noitalic',
    #    'focused-terminal titlebar name':       'bg:#88aa44',
    #    'titlebar.line':                '#444444',
    #    'titlebar.line focused':       '#448844 noinherit',
    #    'titlebar focused':            'bg:#5f875f #ffffff bold',
    #    'titlebar.title focused':      '',
    #    'titlebar.zoom':                'bg:#884400 #ffffff',
    #    'titlebar paneindex':           '',
    #    'titlebar.copymode':            'bg:#88aa88 #444444',
    #    'titlebar.copymode.position':   '',
    #    'focused-terminal titlebar.copymode':          'bg:#aaff44 #000000',
    #    'titlebar.copymode.position': '#888888',
    "commandline": "bg:#4e4e4e #ffffff",
    "commandline.command": "bold",
    "commandline.prompt": "bold",
    #'statusbar':                    'noreverse bg:#448844 #000000',
    "statusbar": "noreverse bg:ansigreen #000000",
    "statusbar window": "#ffffff",
    "statusbar window.current": "bg:#44ff44 #000000",
    "auto-suggestion": "bg:#4e5e4e #88aa88",
    "message": "bg:#bbee88 #222222",
    "background": "#888888",
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
    "cut": "bg:#303030",
    "clock": "bg:#88aa00",
    "panenumber": "bg:#888888",
    "panenumber focused": "bg:#aa8800",
    "terminated": "bg:#aa0000 #ffffff",
    "confirmationtoolbar": "bg:#880000 #ffffff",
    "confirmationtoolbar question": "",
    "confirmationtoolbar yesno": "bg:#440000",
    "copy-mode-cursor-position": "bg:ansiyellow ansiblack",
    #    'search-toolbar':                       'bg:#88ff44 #444444',
    "search-toolbar.prompt": "bg:#88ff44 #444444",
    "search-toolbar.text": "bg:#88ff44 #000000",
    #    'search-toolbar focused':              'bg:#aaff44 #444444',
    #    'search-toolbar.text focused':         'bold #000000',
    "search-match": "#000000 bg:#88aa88",
    "search-match.current": "#000000 bg:#aaffaa underline",
    # The completions. prompt_toolkit draws them on light grey,
    # which is a slab of daylight in the middle of a dark screen
    # once a box holds a whole screenful of them. They take the
    # colours of the box instead, and the row a person is on takes
    # the colour of its title bar.
    "completion-menu": "bg:#1c1c1c #d0d0d0",
    "completion-menu.completion": "bg:#1c1c1c #d0d0d0",
    "completion-menu.completion.current": "bg:#5f5f87 #ffffff",
    "completion-menu.meta.completion": "bg:#262626 #a8a8a8",
    "completion-menu.meta.completion.current": "bg:#5f5f87 #ffffff",
    "scrollbar.background": "bg:#262626",
    "scrollbar.button": "bg:#5f5f87",
    # The ":" command line as a box in the middle of the screen.
    # It borrows the shape of the overlay pane below: a title row
    # over a body. Lillecarl/pymux#158.
    "commandpalette": "bg:#1c1c1c",
    "commandpalette.titlebar": "bg:#5f5f87 #ffffff",
    # The background is named again here. A fragment style is more
    # precise than the style of the window it is drawn in, so
    # `bold` alone left the title on the background of the box and
    # cut a dark hole in the middle of the title bar.
    "commandpalette.title": "bold bg:#5f5f87 #ffffff",
    # The overlay pane, which floats over the layout.
    "overlay": "bg:#1c1c1c",
    "overlay.titlebar": "bg:#5f5f87 #ffffff",
    "overlay.title": "bold",
    # Pop-up dialog. Ignore built-in style.
    "dialog": "noinherit",
    "dialog.body": "noinherit",
    "dialog frame": "noinherit",
    "dialog.body text-area": "noinherit",
    "dialog.body text-area last-line": "noinherit",
}

#: The green scheme pymux has always drawn.
DEFAULT = a_theme(DEFAULT_RULES)

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
    {
        **DEFAULT_RULES,
        "terminal.focused border": "#8787af bold",
        "terminal.focused titlebar": "bg:#5f5f87 #ffffff",
        "terminal.focused titlebar name": "bg:#8787af #ffffff",
        "terminal.focused titlebar paneindex": "bg:#8787af #ffffff",
        "statusbar": "noreverse bg:#5f5f87 #ffffff",
        "statusbar window.current": "bg:#8787af #ffffff",
        "auto-suggestion": "bg:#4e4e5e #8888aa",
        "message": "bg:#8787af #ffffff",
        "clock": "bg:#5f5f87",
        "panenumber focused": "bg:#5f5f87",
        "search-toolbar.prompt": "bg:#8787af #ffffff",
        "search-toolbar.text": "bg:#8787af #000000",
        "search-match": "#000000 bg:#8888aa",
        "search-match.current": "#ffffff bg:#5f5f87 underline",
    }
)

#: Every theme, by the name `set-option theme` takes.
THEMES: dict[str, BaseStyle] = {
    "default": DEFAULT,
    "grey": GREY,
}

#: The one a session starts on.
DEFAULT_THEME = "default"
