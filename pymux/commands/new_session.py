import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.data_structures import Size

from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import add_format_arguments, print_object_format
from pymux.enums import Woke
from pymux.session import DEFAULT_SIZE


def _axis(given: str | None, standing: int, watching: int) -> int:
    """
    One axis of the size a session has while nobody watches it.

    Nothing given keeps what the session already has. A number is that
    number. "-" is the size of the client that asked, and the standing
    answer when no client asked -- which is what tmux reads it as
    (`cmd-new-session.c`: `dsx = c->tty.sx` or 80).
    """
    if given is None:
        return standing
    if given == "-":
        return watching if watching else standing
    try:
        wanted = int(given)
    except ValueError:
        raise CommandException("Expecting an integer: %s" % (given,))
    if wanted < 1:
        raise CommandException("A window is at least one cell.")
    return wanted


def _size_with_no_client(pymux: "Pymux", args: argparse.Namespace) -> Size:
    "What `-x` and `-y` say about a session nobody is watching."
    watching = None
    try:
        client_state = pymux.get_client_state()
    except ValueError:
        pass
    else:
        if not client_state.temporary:
            watching = client_state.app.output.get_size()

    return Size(
        rows=_axis(args.rows, DEFAULT_SIZE.rows, watching.rows if watching else 0),
        columns=_axis(
            args.columns, DEFAULT_SIZE.columns, watching.columns if watching else 0
        ),
    )


def new_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Create a session on this server.

    The client that asks moves to the new session, the way tmux
    attaches one. `-d` leaves the client where it is.

    A command that arrived over the socket runs under a client that
    draws nothing, so there is nothing to attach: the session is made
    and the command answers, which is `-d` in all but name.

    **`-x` and `-y` say how big a window of this session is while
    nobody is watching it**, which for a session nothing ever attaches
    to is how big it is. Without them it is eighty by twenty-four, and
    a program in a pane laid itself out at that whatever the caller
    meant. The size is set before the first pane starts, so the program
    is never told the wrong one. Lillecarl/pymux#459.

    It is not `resize-window`: a manual size says a person means that
    size to stay, so a client that attaches keeps it and scrolls. This
    one gives way to the client, and tmux reads the same two flags the
    same way.
    """
    name = args.session_name

    if name is not None and pymux.get_session(name) is not None:
        raise CommandException("duplicate session: %s" % (name,))

    # Read before anything is made, so that a size that cannot be read
    # leaves no half-made session behind.
    size = _size_with_no_client(pymux, args)

    session = pymux.create_session(name=name)
    session.default_size = size
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
    parser.add_argument("-x", dest="columns", metavar="<columns>", help="How many columns a window is while nobody watches it. \"-\" is the size of the client that asks.")
    parser.add_argument("-y", dest="rows", metavar="<rows>", help="How many rows a window is while nobody watches it. \"-\" is the size of the client that asks.")
    parser.add_argument("-P", dest="P", action="store_true", help="Print information about the session.")
    add_format_arguments(parser, "The format to print with -P.")
    parser.add_argument("command", metavar="<shell-command>", nargs="?", help="What the first pane runs.")
