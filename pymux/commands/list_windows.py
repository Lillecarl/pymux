import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.common import session_part, show_listing
from pymux.format import Language, format_pymux_string


def _sessions(pymux: "Pymux", args: argparse.Namespace) -> list:
    """
    The sessions this listing covers.

    `-a` is every session of the server. A `-t` with a session part
    names one. Without either it is the session the client is on.
    """
    if args.a:
        return list(pymux.sessions)

    if args.target_window:
        session, _ = session_part(pymux, args.target_window)
        if session is None:
            raise CommandException("can't find session: %s" % (args.target_window,))
        return [session]

    return [pymux.current_session]


def list_windows(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    List the windows of the session.

    With `-F`, the formatted window information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    sessions = _sessions(pymux, args)

    if args.format:
        format_str = args.format or "#{window_id}"
        for session in sessions:
            for w in session.arrangement.windows:
                pymux.print_command_line(
                    format_pymux_string(
                        pymux,
                        format_str,
                        window=w,
                        pane=w.active_pane,
                        session=session,
                        # `-F` is read by a script: tmux format only.
                        # Lillecarl/pymux#333.
                        language=Language.TMUX,
                    )
                )
    else:
        windows = [
            window for session in sessions for window in session.arrangement.windows
        ]
        w = sessions[0].arrangement.get_active_window()
        result = []
        for window in windows:
            # The size of this window's own pane. Every row used to
            # carry the size of the active window's pane, which read as
            # every window being the same size.
            process = window.active_pane.process
            result.append(
                "%i %s%s [%sx%s]"
                % (
                    window.index,
                    window.name,
                    " (active)" if window == w else "",
                    process.sx,
                    process.sy,
                )
            )
        show_listing(pymux, "list-windows", "\n".join(result))


def register(subparsers):
    parser = add_command(subparsers, list_windows)
    parser.add_argument("-a", dest="a", action="store_true", help="Every window of every session.")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The session whose windows to list.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="Print this format for every window.")
