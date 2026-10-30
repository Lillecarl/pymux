from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.client.uploads import LOCAL_CLIENT_OPTIONS
from pymux.commands import CommandException, CommandParser, add_command, not_past_this_client, this_client
from pymux.commands.common import clients_named
from pymux.commands.set_option import set_option
from pymux.options.options import Scope


def set_client_option(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Set an option of one attached terminal.

    -t: the client, as `list-clients` prints it first on a line.
        Without it, the client this command came from -- and a command
        typed in a pane comes from the client that owns the pane, not
        from the temporary one the server made to run it.
        Lillecarl/pymux#272.

    **A configuration file sets these too, and the client reads it.** A
    client option belongs to the terminal a person is sitting at, so
    the client finds its own configuration file and announces what the
    `set-client-option` lines in it say when it attaches. Over SSH
    those are two files on two machines, which is the point: the panes
    run there and the screen is here.

    So a `set-client-option` line the server reads is not an error and
    does nothing: the server is reading a file for a client that does
    not exist yet, and that client will read the same kind of line
    itself. Lillecarl/pymux#223.

    **What this sets dies with the attachment.** A client announces its
    configuration every time it attaches, a reconnect included, so a
    value set here lasts as long as the terminal stays connected.
    Lillecarl/pymux#256, Lillecarl/pymux#340.

    **A client that only watches may set its own.** All three of these
    options are facts of the terminal a person is sitting at -- what
    it is called, the colours it draws with, and whether they go the
    other way round -- so refusing them costs a watcher their screen
    and protects nobody. tmux has no per-client option to compare
    with. `-t` is the part that reaches somebody else.
    Lillecarl/pymux#467.
    """
    if pymux.sourcing and pymux.the_client_to_tell() is None:
        # The server, reading a configuration file with nobody
        # attached. #199 is the same shape: a command that raises here
        # takes `source-file` with it, and pymux draws nothing at all.
        return

    # **The upload options are the client's alone.** They say which of
    # the person's files a pane may read without asking, and the server
    # is the side a program in a pane can control. The option system
    # has no "only from the client's own file", so they never reach it:
    # the client reads them itself and does not announce them.
    # Lillecarl/pymux#743.
    if args.option in LOCAL_CLIENT_OPTIONS:
        if pymux.sourcing:
            return
        raise CommandException(
            "%s is read only from the configuration file on the client's own machine." % (args.option,)
        )

    target = None
    if args.target_client is not None:
        target = clients_named(pymux, args.target_client)[0]
        not_past_this_client(pymux, target is not this_client(pymux))

    set_option(pymux, args, scope=Scope.CLIENT, target=target)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, set_client_option, read_only=True)
    parser.add_argument(
        "-t",
        dest="target_client",
        metavar="<target-client>",
        help="The client of this name, as list-clients prints it.",
    )
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")
