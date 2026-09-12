import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing
from pymux.format import format_pymux_string


def list_windows(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    List the windows of the session.

    With `-F`, the formatted window information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if args.format:
        format_str = args.format or "#{window_id}"
        lines = [
            format_pymux_string(pymux, format_str, window=w, pane=w.active_pane)
            for w in pymux.arrangement.windows
        ]
        pymux.print_command_line("\n".join(lines))
    else:
        w = pymux.arrangement.get_active_window()
        result = []
        for i, window in enumerate(pymux.arrangement.windows):
            result.append(
                "%i %s%s [%sx%s]"
                % (
                    window.index,
                    window.name,
                    " (active)" if window == w else "",
                    w.active_pane.process.sx,
                    w.active_pane.process.sy,
                )
            )
        show_listing(pymux, "list-windows", "\n".join(result))


def register(subparsers):
    parser = add_command(subparsers, list_windows)
    parser.add_argument("-a", dest="a", action="store_true", help="Every window, not only of the session.")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to list.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="Print this format for every window.")
