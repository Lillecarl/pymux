from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pymux.arrangement import Pane
    from pymux.main import Pymux
    from pymux.session import Session


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import (
    add_format_arguments,
    chosen_format,
    session_part,
    show_listing,
)
from pymux.format import format_pymux_string


def _sessions(pymux: Pymux, args: argparse.Namespace) -> list[Session]:
    """
    The sessions this listing covers.

    `-a` is every session of the server. A `-t` names one, as tmux's
    target-session does: a bare name is a session and not a window of
    this one, and a `session:window` target keeps its session part.
    Without either it is the session the client is on.
    Lillecarl/pymux#550.
    """
    if args.a:
        return list(pymux.sessions)

    target = args.target_session
    if target:
        if ":" in target:
            session, _ = session_part(pymux, target)
        else:
            session = pymux.get_session(target)
        if session is None:
            raise CommandException("can't find session: %s" % (target,))
        return [session]

    return [pymux.current_session]


def list_windows(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    List the windows of the session.

    With `-F`, the formatted window information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    sessions = _sessions(pymux, args)

    chosen = chosen_format(args, "#{window_id}")

    if chosen.asked:
        for session in sessions:
            for w in session.arrangement.windows:
                pymux.print_command_line(
                    format_pymux_string(
                        pymux,
                        chosen.string,
                        window=w,
                        pane=w.active_pane,
                        session=session,
                        language=chosen.language,
                    )
                )
    else:
        windows = [window for session in sessions for window in session.arrangement.windows]
        w = sessions[0].arrangement.get_active_window()
        result = []
        for window in windows:
            # The size of this window's own pane. Every row used to
            # carry the size of the active window's pane, which read as
            # every window being the same size.
            process = cast("Pane", window.active_pane).process
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


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, list_windows)
    parser.add_argument("-a", dest="a", action="store_true", help="Every window of every session.")
    parser.add_argument(
        "-t", dest="target_session", metavar="<target-session>", help="The session whose windows to list."
    )
    add_format_arguments(parser, "Print this format for every window.")
