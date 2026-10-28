"""
The options of a session, each declared once.

`Pymux.session_defaults` holds one value of each, which every session
reads unless it set its own; `Session.options` is that session's view.
`set-option -g` writes the defaults, and `set-option` without it the
session's own. Lillecarl/pymux#756. `pymux/option_fields.py` says what
the declaration gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyte.keys import KEY_PROFILES

from .enums import EditingKeys, Justify, OpenUrlMode, OpenUrlTarget, PaneBrowser
from .option_fields import Options, option, options
from .utils import editor_keys, get_default_shell

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["DEFAULT_UPDATE_ENVIRONMENT", "SessionOptions"]

#: tmux's own list, which is every name that points at something on
#: the machine a person is sitting at: a display, an agent, a session
#: bus. ("update-environment", `options-table.c`.) Lillecarl/pymux#271.
DEFAULT_UPDATE_ENVIRONMENT = (
    "DISPLAY KRB5CCNAME MSYSTEM SSH_ASKPASS SSH_AUTH_SOCK SSH_AGENT_PID "
    "SSH_CONNECTION WAYLAND_DISPLAY WINDOWID XAUTHORITY XDG_CURRENT_DESKTOP "
    "XDG_SESSION_DESKTOP XDG_SESSION_TYPE"
)


def _key_profile(value: str) -> str:
    if value not in KEY_PROFILES:
        raise ValueError("Expecting one of: %s." % ", ".join(sorted(KEY_PROFILES)))
    return value


def _renumber(pymux: Pymux, owner: object) -> None:
    # Setting it on closes the gaps that are open already; off changes
    # nothing, because no setting opens one. A default reaches every
    # session without its own value. Lillecarl/pymux#342.
    for session in pymux.sessions:
        if session.arrangement.renumber_windows:
            session.arrangement.renumber()


def _recolor(pymux: Pymux, owner: object) -> None:
    # A pane's colour base comes from which theme owns the screen, and
    # whether it does. A pane that exists heard the old answer.
    # Lillecarl/pymux#283.
    pymux.terminals.sync_color_bases()


@options
class SessionOptions(Options):
    base_index: int = option(
        1,
        doc=(
            "The number of the first window. tmux starts at zero, but the 1 key is easier to reach "
            "and sits where the first window belongs."
        ),
        choices=[0, 1],
    )
    renumber_windows: bool = option(
        False,
        doc=(
            "Whether closing a window packs the ones above it down, so the order has no gaps. Off, "
            "the way tmux ships it: window 4 stays window 4 until a person says otherwise. Lillecarl/pymux#342."
        ),
        on_set=_renumber,
    )
    bell: bool = option(True, doc="Whether a bell in a pane rings the terminal of the person.")
    open_url_target: OpenUrlTarget = option(
        OpenUrlTarget.LAST, doc="Which client's browser a URL opens in. `OpenUrlTarget` says."
    )
    open_url_mode: OpenUrlMode = option(
        OpenUrlMode.OPEN, doc="What happens to a request to open a URL. `OpenUrlMode` says."
    )
    open_url_shim: bool = option(
        False,
        doc=(
            'Put the opener of this session on the PATH of a pane, as "xdg-open", and name it in '
            "$BROWSER, so a program in a pane opens a browser here without knowing pymux. Only new panes see it."
        ),
    )
    pane_browser: PaneBrowser = option(
        PaneBrowser.AGENTS,
        doc=(
            'Which programs in a pane find `pymux-browser` as their browser: "agents", "all" or "off". '
            "`PaneBrowser` says why. Only new panes see it. Lillecarl/pymux#653."
        ),
    )
    notify_shim: bool = option(
        False,
        doc=(
            'Put the notifier of this session on the PATH of a pane, as "notify-send", so a program '
            "notifies the hub without knowing pymux. Only new panes see it."
        ),
    )
    open_url_forward: bool = option(
        True,
        doc=(
            "Bring the port with a loopback URL: localhost on the browser of the machine at the "
            "keyboard means a service on that machine, which is the wrong one. Lillecarl/pymux#437."
        ),
    )
    open_url_forward_idle: int = option(
        600,
        doc=(
            "How long such a forward outlives its last connection, in seconds. A forward that carries "
            "a connection is never idle; ten minutes leaves room for a tab somebody comes back to."
        ),
        choices=[60, 300, 600, 3600],
    )
    history_limit: int = option(
        2000,
        doc="How many lines above the screen a pane keeps: tmux's two thousand. Lillecarl/pymux#8.",
        choices=[200, 500, 1000, 2000, 5000, 10000],
    )
    strip_animation_time: int = option(
        150,
        doc="How long a strip's view glides to the column a person moved to, in milliseconds. Zero jumps.",
        choices=[0, 100, 150, 250],
    )
    cut_shade_width: int = option(
        4,
        doc="How many cells the shade fades over where the view cuts a pane; zero for none. Lillecarl/pymux#616.",
        choices=[0, 2, 4, 6, 8],
    )
    cut_shade_strength: int = option(
        14,
        doc="How far, in percent, the cell at a cut edge moves towards black or white. 8 was a bit faint.",
        choices=[8, 12, 16, 24],
    )
    mouse: bool = option(True, doc="Whether a click, a drag and a scroll reach pymux.")
    remain_on_exit: bool = option(False, doc="Keep a pane on the screen after its program ended.")
    status: bool = option(True, doc="Draw the status line.")
    pane_border_status: bool = option(True, doc="Draw a title bar on each pane.")
    command_palette: bool = option(
        False,
        doc=(
            'Draw the ":" command line as a box in the middle of the screen, with room for what a '
            "completion means and a usage line. Off, so the bar does not move unasked. Lillecarl/pymux#158."
        ),
    )
    which_key: bool = option(
        False,
        doc="Draw the keys a prefix leads to while it waits. Off, for a person who knows them. Lillecarl/pymux#29.",
    )
    chooser_session_lines: bool = option(
        True,
        doc="Start each session on a line of its own in the window chooser, so sessions read as groups. Lillecarl/pymux#531.",
    )
    status_keys: EditingKeys = option(
        factory=editor_keys,
        doc=(
            "Which keys the command line takes. The person's $EDITOR says, as in tmux: a vi gives vi "
            "keys, hx gives Helix keys. Lillecarl/pymux#375, Lillecarl/pymux#749."
        ),
    )
    mode_keys: EditingKeys = option(
        factory=editor_keys,
        doc="Which keys copy mode takes, from $EDITOR like `status-keys`. Lillecarl/pymux#375.",
    )
    steal_focus: bool = option(
        False,
        doc=(
            "Whether a program's OSC 1337 StealFocus takes each client on the session to its pane. Off, "
            "because a program that moves people at will is a nuisance. Lillecarl/pymux#785."
        ),
    )
    notification_click_shows_pane: bool = option(
        True,
        doc=(
            "A click on a desktop notification takes the clicked client to the pane it came from, "
            "where the terminal reports clicks (OSC 99). Lillecarl/pymux#779."
        ),
    )
    copy_mode_wheel_lines: int = option(
        1,
        doc="How many lines one notch of the wheel scrolls copy mode. tmux binds five.",
        choices=[1, 3, 5],
    )
    lock_command: str = option(
        "vlock", doc="What lock-server, lock-session and lock-client run to hold the keyboard.", choices=["vlock"]
    )
    status_right: str = option(" %H:%M %d-%b-%y ", doc="What the right of the status line says.")
    status_left: str = option("[#S] ", doc="What the left of the status line says.")
    status_right_length: int = option(
        40,
        doc="The most cells the right of the status line takes. At 20 a rate beside the clock pushed the clock off.",
        choices=[40],
    )
    status_left_length: int = option(20, doc="The most cells the left of the status line takes.", choices=[20])
    status_max_rows: int = option(
        3,
        doc="The most rows the window list wraps to; a window past the last row is cut. Lillecarl/pymux#723.",
        choices=[1, 2, 3, 5],
    )
    window_status_format: str = option("#I:#W#F", doc="How the status line names a window.")
    window_status_current_format: str = option("#I:#W#F", doc="How the status line names the active window.")
    bell_symbol: str = option(
        "!",
        doc="What `#F` writes for a bell. ASCII, so every font has it; a configuration may set icons. Lillecarl/pymux#767.",
    )
    activity_symbol: str = option("#", doc="What `#F` writes for activity. Lillecarl/pymux#767.")
    silence_symbol: str = option("~", doc="What `#F` writes for silence. Lillecarl/pymux#767.")
    notification_symbol: str = option("?", doc="What `#F` writes for a notification. Lillecarl/pymux#767.")
    default_shell: str = option(factory=get_default_shell, doc="What a new pane runs when no command is given.")
    status_justify: Justify = option(Justify.LEFT, doc="Where the window list sits in the status line.")
    status_interval: int = option(
        4,
        doc="Seconds between two redraws of the status line: tmux's four. It is what the clock costs when nothing happens.",
        choices=[1, 2, 4, 8, 16, 30, 60],
    )
    update_environment: str = option(
        DEFAULT_UPDATE_ENVIRONMENT,
        doc=(
            "The names a client brings with it, space separated, each one a pattern. "
            "`Pymux.take_environment_from` says what happens to them. Lillecarl/pymux#271."
        ),
    )
    synthesize_key_events: bool = option(
        True,
        doc=(
            "Make up the halves of a key event that the keyboard of a client cannot send, so a pane "
            "gets the keyboard protocol whole from any terminal."
        ),
    )
    key_profile: str = option(
        "kitty",
        doc=(
            "Whose answer a program in the legacy encoding gets where the terminals split, for a client "
            "whose terminal pymux cannot name. Lillecarl/pymux#706."
        ),
        choices=KEY_PROFILES,
        parse=_key_profile,
    )
    allow_program_resize: bool = option(
        False,
        doc="May a program inside a pane resize that pane? Off: making one taller makes another shorter.",
    )
    paint_screen: bool = option(
        False,
        doc=(
            "Draw the theme's own background behind every cell, so a theme is the colour of the whole "
            "screen. Off: a program that relies on the terminal's background keeps it. "
            "Lillecarl/pymux#273, Lillecarl/pymux#283."
        ),
        on_set=_recolor,
    )
