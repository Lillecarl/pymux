from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Window
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import add_format_arguments, chosen_format, find_window, show_listing, the_window
from pymux.format import format_pymux_string


def list_panes(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Display a list of all the panes.

    Without `-F`, this displays the pane overview as a pop-up in the user
    interface. With `-F`, the formatted pane information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if args.target_pane:
        window = find_window(pymux, args.target_pane.rsplit(".", 1)[0])
        if window is None:
            raise CommandException("can't find window: %s" % (args.target_pane,))
        windows: list[Window] = [window]
    elif args.a:
        # Every window of every session. tmux reads `-a` as the whole
        # server too. Lillecarl/pymux#323.
        windows = [w for session in pymux.sessions for w in session.arrangement.windows]
    elif args.s:
        windows = list(pymux.arrangement.windows)
    else:
        windows = [the_window(pymux, None)]

    active_pane = windows[0].active_pane

    chosen = chosen_format(args, "#{pane_id}")

    if chosen.asked:
        # Print one line for every pane.
        for w in windows:
            session = pymux.session_of_window(w)
            for p in w.panes:
                pymux.print_command_line(
                    format_pymux_string(
                        pymux,
                        chosen.string,
                        window=w,
                        pane=p,
                        session=session,
                        language=chosen.language,
                    )
                )
    else:
        result = []

        for i, p in enumerate(windows[0].panes):
            process = p.process

            result.append(
                "%i: [%sx%s] [history %s/%s] %s"
                % (
                    i,
                    process.sx,
                    process.sy,
                    min(pymux.history_limit, p.screen.line_offset + process.sy),
                    pymux.history_limit,
                    ("(active)" if p == active_pane else ""),
                )
            )

        # The list-keys title rode along when this branch was
        # written, and the overview of panes said list-keys.
        # Lillecarl/pymux#288.
        show_listing(pymux, "list-panes", "\n".join(sorted(result)))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, list_panes)
    parser.add_argument("-a", dest="a", action="store_true", help="The panes of every window of every session.")
    parser.add_argument("-s", dest="s", action="store_true", help="The panes of every window of this session.")
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane whose window to list.")
    add_format_arguments(parser, "Print this format for every pane.")
