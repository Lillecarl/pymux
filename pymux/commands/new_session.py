import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import add_format_arguments, print_object_format
from pymux.enums import Woke


def new_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Create a session on this server.

    The client that asks moves to the new session, the way tmux
    attaches one. `-d` leaves the client where it is.

    A command that arrived over the socket runs under a client that
    draws nothing, so there is nothing to attach: the session is made
    and the command answers, which is `-d` in all but name.
    """
    name = args.session_name

    if name is not None and pymux.get_session(name) is not None:
        raise CommandException("duplicate session: %s" % (name,))

    session = pymux.create_session(name=name)
    pymux.create_window(
        command=args.command,
        start_directory=args.start_directory,
        name=args.window_name,
        session=session,
    )
    pymux.invalidate(Woke.SESSION_OPENED)

    if not args.d:
        try:
            client_state = pymux.get_client_state()
        except ValueError:
            client_state = None

        if client_state is not None and not client_state.temporary:
            pymux.attach_client_to(client_state, session)

    if args.P:
        window = session.arrangement.get_active_window()
        print_object_format(
            pymux,
            args,
            window=window,
            pane=window.active_pane,
            session=session,
        )


def register(subparsers):
    parser = add_command(subparsers, new_session)
    parser.add_argument("-s", dest="session_name", metavar="<session-name>", help="The name of the session.")
    parser.add_argument("-n", dest="window_name", metavar="<window-name>", help="The name of the first window.")
    parser.add_argument("-c", dest="start_directory", metavar="<start-directory>", help="The working directory of the first pane.")
    parser.add_argument("-d", dest="d", action="store_true", help="Do not attach.")
    parser.add_argument("-P", dest="P", action="store_true", help="Print information about the session.")
    add_format_arguments(parser, "The format to print with -P.")
    parser.add_argument("command", metavar="<shell-command>", nargs="?", help="What the first pane runs.")
