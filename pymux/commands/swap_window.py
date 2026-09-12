import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import find_window


def swap_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Swap the active window with the window a target names.

    The windows trade indexes, and without `-d` the window swapped
    into the active one's place takes the focus, the way tmux's does.
    A relative target, `+1` or `-1`, counts from the active window:
    the tmux spell is `swap-window -t -1`. Lillecarl/pymux#296.
    """
    dst = args.dst_window
    active = pymux.arrangement.get_active_window()

    if dst.startswith(("+", "-")):
        dst_window = pymux.arrangement.get_window_by_index(active.index + int(dst))
    else:
        dst_window = find_window(pymux, dst)

    if dst_window is None or dst_window is active:
        return

    pymux.arrangement.swap_window(active, dst_window)

    if not args.d:
        pymux.arrangement.set_active_window(dst_window)


def register(subparsers):
    parser = add_command(subparsers, swap_window)
    parser.add_argument(
        "-d", dest="d",
        action="store_true",
        help="Keep the active window active. The windows trade places either way.",
    )
    parser.add_argument("-t", dest="dst_window", metavar="<dst-window>", required=True, help="The index to swap with. `+1` and `-1` count from the active window.")
