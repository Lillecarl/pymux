"""
The options of one window, each declared once. `set-window-option`
writes them, and `-g` says what a new window starts with.
`pymux/option_fields.py` says what the declaration gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import WindowCodec, WindowSize
from .option_fields import Options, option, options
from .wayland_scale import DEFAULT_WAYLAND_SCALE, parse_scale

if TYPE_CHECKING:
    from .arrangement import Window
    from .main import Pymux

__all__ = ["DEFAULT_FRAME_RATE", "WindowOptions"]

#: The most frames a second a client draws, until somebody says
#: otherwise. Zero means as many as it can.
#:
#: **Thirty, because a frame is expensive and an eye is not fast.** A
#: frame at 187x59 costs about 19 ms of CPU, nearly all of it building
#: cells that a diff then throws away (Lillecarl/pymux#254). A pane
#: that animates asks for one on every write, and three of them
#: together asked for seventeen a second. Thirty is above what a person
#: reads text at and below what a program writes at, so it costs
#: nothing anybody sees and refuses the frames nobody does.
#:
#: It is not a latency cap. A keystroke's frame is drawn at once
#: whenever the last one was more than a thirtieth of a second ago,
#: which at a human typing speed is always.
DEFAULT_FRAME_RATE = 30


def _shape_for_strip(window: Window) -> None:
    window.shape_for_strip()


def _freeze_manual(pymux: Pymux, window: Window) -> None:
    # **`manual` with no size freezes the window as it is.** A person
    # who says it and nothing else means "stop following the clients",
    # not "pick a size for me", and tmux does the same. `resize-window`
    # is how a size is named.
    if window.options.window_size is WindowSize.MANUAL and window.manual_size is None:
        window.manual_size = pymux.plane_size(window)


def _frame_rate_chosen(pymux: Pymux, window: Window) -> None:
    window.frame_rate_set = True


def _rescale(pymux: Pymux, window: Window) -> None:
    pymux.wayland.rescale()


def _redraw(pymux: Pymux, window: Window) -> None:
    # The next frame of each client goes the new way.
    pymux.invalidate()


@options
class WindowOptions(Options):
    synchronize_panes: bool = option(False, doc="Send what is typed to every pane of the window.")
    pane_title_format: str = option(
        " #T  #{pane_current_path_compact} ",
        doc=(
            "What the bar over each pane says, after its marks and its name: its title, and its "
            "directory the way fish's prompt writes it, which stays after the program ends."
        ),
        on_set=_redraw,
    )
    monitor_bell: bool = option(
        True,
        doc="Whether a bell nobody saw flags the window and the pane it rang in. Lillecarl/pymux#501.",
    )
    monitor_activity: bool = option(
        True,
        doc="Whether output in a window nobody looks at flags it. On, where tmux has it off. Lillecarl/pymux#767.",
    )
    monitor_silence: int = option(
        0,
        doc="Flag a window nobody looks at after so many quiet seconds that follow output. Zero is off.",
        choices=[0, 10, 30, 60],
    )
    monitor_notification: bool = option(
        True, doc="Whether a notification from a window nobody looks at flags it. Lillecarl/pymux#767."
    )
    strip: bool = option(
        False,
        doc=(
            "Lay the panes out as a strip that may run past the edge of the screen, the way niri's "
            "scrollable tiling works, instead of dividing the window between them. "
            "`select-layout` turns it off. Lillecarl/pymux#198."
        ),
        on_write=_shape_for_strip,
    )
    window_size: WindowSize = option(
        WindowSize.SMALLEST,
        doc=(
            "Which client's terminal decides how big the window's plane is, when more than one watches it. "
            '"smallest" shows every client the whole window; "largest" gives it to the biggest client, '
            "and a smaller one moves its view over it. Lillecarl/pymux#217."
        ),
        on_set=_freeze_manual,
    )
    frame_rate: int = option(
        DEFAULT_FRAME_RATE,
        doc=(
            "The most frames a second a client draws while it looks at the window. Zero is as many as "
            "it can. Setting it also caps the Wayland windows in it. Lillecarl/pymux#254, Lillecarl/pymux#636."
        ),
        choices=[0, 10, 30, 60, 120],
        on_set=_frame_rate_chosen,
    )
    wayland_scale: str = option(
        DEFAULT_WAYLAND_SCALE,
        doc="How dense the Wayland windows in it draw: `auto` reads it off the client's cell. Lillecarl/pymux#637.",
        choices=["auto", "auto+0.5", "auto*1.5", "1", "2"],
        parse=parse_scale,
        on_set=_rescale,
    )
    window_codec: WindowCodec = option(
        WindowCodec.LOSSLESS,
        doc="Whether its Wayland windows reach a client as the pixels that changed or as VP9. Lillecarl/pymux#639.",
        on_set=_redraw,
    )
