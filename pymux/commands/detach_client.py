import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import get_app
from pymux.commands import CommandException, add_command


def _detach(pymux: "Pymux", client_state) -> None:
    """
    Detach one client that is not necessarily this one.

    `Pymux.detach_client` takes the connection of the application that
    is current, which is the only client a command could reach before
    this. Lillecarl/pymux#335.
    """
    connection = getattr(client_state, "connection", None)
    if connection is not None:
        connection.detach_and_close()


def detach_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Detach a client from its session. The panes stay with the server.

    `-s` detaches every client watching that session, this one
    included, which is how a person hands a session over. `-a`
    detaches every client but this one, which is what somebody wants
    when they find a session attached twice and they are at the
    terminal they mean to keep.

    `-t <client>` detaches the one client of that name, which
    `list-clients` prints first on every line.
    Lillecarl/pymux#335.
    """
    if args.target_client is not None:
        wanted = args.target_client
        found = [
            client_state
            for client_state in pymux.clients
            if getattr(client_state.connection, "name", "") == wanted
        ]
        if not found:
            raise CommandException("can't find client: %s" % (wanted,))
        for client_state in found:
            _detach(pymux, client_state)
        return

    if args.target_session is not None:
        session = pymux.get_session(args.target_session)
        if session is None:
            raise CommandException(
                "can't find session: %s" % (args.target_session,)
            )
        for client_state in pymux.clients:
            if client_state.session is session:
                _detach(pymux, client_state)
        return

    if args.all_but_this_one:
        # **Not `get_client_state`.** A command that arrived from a
        # pane's CLI runs under a temporary client, and that one is
        # nobody: keeping it would detach every terminal a person is
        # actually sitting at. `the_client_to_tell` is the rule pymux
        # already has for this (Lillecarl/pymux#272) and the faithful
        # reading of tmux's target client, which for a command from a
        # pane is the client that owns the pane.
        here = pymux.the_client_to_tell()
        # tmux counts a client attached to nothing as already detached
        # and leaves it alone: `loop->session != NULL`.
        for client_state in pymux.clients:
            if client_state is not here and client_state.session is not None:
                _detach(pymux, client_state)
        return

    pymux.detach_client(get_app())


def register(subparsers):
    parser = add_command(subparsers, detach_client)
    parser.add_argument("-a", dest="all_but_this_one", action="store_true", help="Every client but this one.")
    parser.add_argument("-s", dest="target_session", metavar="<target-session>", help="Every client watching this session.")
    parser.add_argument("-t", dest="target_client", metavar="<target-client>", help="The client of this name, as list-clients prints it.")
