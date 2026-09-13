import argparse
from typing import List
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux
    from pymux.arrangement import Window


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_window
from pymux.commands.common import show_listing
from pymux.format import Language, format_pymux_string


def list_panes(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Display a list of all the panes.

    Without `-F`, this displays the pane overview as a pop-up in the user
    interface. With `-F`, the formatted pane information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if args.target_pane:
        window = find_window(pymux, args.target_pane.rsplit(".", 1)[0])
        if window is None:
            raise CommandException(
                "Can't find window: %s" % (args.target_pane,)
            )
        windows: List["Window"] = [window]
    elif args.a:
        # Every window of every session. tmux reads `-a` as the whole
        # server too. Lillecarl/pymux#323.
        windows = [
            w for session in pymux.sessions for w in session.arrangement.windows
        ]
    elif args.s:
        windows = list(pymux.arrangement.windows)
    else:
        windows = [pymux.arrangement.get_active_window()]

    active_pane = windows[0].active_pane

    if args.format:
        # Print one line for every pane.
        format_str = args.format or "#{pane_id}"
        for w in windows:
            session = pymux.session_of_window(w)
            for p in w.panes:
                pymux.print_command_line(
                    format_pymux_string(
                        pymux,
                        format_str,
                        window=w,
                        pane=p,
                        session=session,
                        # `-F` is read by a script: tmux format only.
                        # Lillecarl/pymux#333.
                        language=Language.TMUX,
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


def register(subparsers):
    parser = add_command(subparsers, list_panes)
    parser.add_argument("-a", dest="a", action="store_true", help="The panes of every window of every session.")
    parser.add_argument("-s", dest="s", action="store_true", help="The panes of every window of this session.")
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane whose window to list.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="Print this format for every pane.")
