import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing
from pymux.format import Language, format_pymux_string

#: What a line says when nobody asked for a format. tmux writes the
#: name, the window count and whether anybody is on it.
DEFAULT_FORMAT = "#{session_name}: #{session_windows} windows"


def list_sessions(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    List the sessions of this server, one to a line.

    With `-F`, each line is that format. (Like tmux.)
    """
    format_str = args.format or DEFAULT_FORMAT

    lines = []
    for session in pymux.sessions:
        windows = session.arrangement.windows
        window = windows[0] if windows else None
        lines.append(
            format_pymux_string(
                pymux,
                format_str,
                window=window,
                pane=window.active_pane if window is not None else None,
                session=session,
                # A listing is read by a script, so `-F` is tmux format
                # and nothing else. Lillecarl/pymux#333.
                language=Language.TMUX,
            )
        )

    if args.format:
        # One line to a session, the way tmux answers. A reader that
        # enumerates the sessions splits the answer on newlines, so a
        # single string with newlines in it would read as one session
        # with a strange name.
        for line in lines:
            pymux.print_command_line(line)
    else:
        show_listing(pymux, "list-sessions", "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, list_sessions, aliases=("ls",))
    parser.add_argument("-a", dest="a", action="store_true", help="Accepted for tmux. Every session of this server is listed anyway.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="Print this format for each session.")
