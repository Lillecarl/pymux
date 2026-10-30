from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux
    from pymux.core.window_tree import Window


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import (
    add_format_arguments,
    add_tag_filter,
    chosen_format,
    find_window,
    session_of_target,
    show_listing,
    the_window,
)
from pymux.format import format_pymux_string
from pymux.tags import carries, parse_tag


def list_panes(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Display a list of all the panes.

    Without `-F`, this displays the pane overview as a pop-up in the user
    interface. With `-F`, the formatted pane information is printed to the
    output of the pymux command line. (Like tmux.)

    `--tag` keeps the panes carrying every tag it names, and with no
    `-t` or `-s` it looks through the whole server, as `-a` does: a tag
    finds a pane wherever it is.
    """
    wanted = [parse_tag(word) for word in args.tags]
    if args.a:
        # Every window of every session. tmux reads `-a` as the whole
        # server too. Lillecarl/pymux#323.
        windows: list[Window] = [w for session in pymux.sessions for w in session.arrangement.windows]
    elif args.s:
        # With `-s` the target is a session, as tmux reads it
        # (`cmd-list-panes.c`). Lillecarl/pymux#808.
        windows = list(session_of_target(pymux, args.target_pane).arrangement.windows)
    elif args.target_pane:
        window = find_window(pymux, args.target_pane.rsplit(".", 1)[0])
        if window is None:
            raise CommandException("can't find window: %s" % (args.target_pane,))
        windows = [window]
    elif wanted:
        windows = [w for session in pymux.sessions for w in session.arrangement.windows]
    else:
        windows = [the_window(pymux, None)]

    chosen = chosen_format(args, "#{pane_id}")

    if chosen.asked or wanted:
        # Print one line for every pane.
        for w in windows:
            session = pymux.session_of_window(w)
            for p in w.panes:
                if not carries(p.tags, wanted):
                    continue
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
        active_pane = windows[0].active_pane
        result = []

        for i, p in enumerate(windows[0].panes):
            process = p.process

            result.append(
                "%i: [%sx%s] [history %s/%s] %s"
                % (
                    i,
                    process.sx,
                    process.sy,
                    min(pymux.current_session.options.history_limit, p.screen.line_offset + process.sy),
                    pymux.current_session.options.history_limit,
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
    add_tag_filter(parser, "panes")
