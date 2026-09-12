import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing
from pymux.format import format_pymux_string


def list_sessions(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    List the session of this server.

    With `-F`, the formatted session information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if args.format:
        format_str = args.format
        window = pymux.arrangement.get_active_window()
        line = format_pymux_string(
            pymux, format_str, window=window, pane=window.active_pane
        )
        pymux.print_command_line(line)
    else:
        show_listing(
            pymux, "list-sessions", format_pymux_string(pymux, "#{session_name}")
        )


def register(subparsers):
    parser = add_command(subparsers, list_sessions, aliases=("ls",))
    parser.add_argument("-a", dest="a", action="store_true", help="Accepted for tmux. Pymux has one session per server.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="Print this format for the session.")
