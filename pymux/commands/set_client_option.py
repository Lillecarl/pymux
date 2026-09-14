import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import clients_named
from pymux.commands.set_option import set_option
from pymux.options import Scope


def set_client_option(pymux: "Pymux", args: argparse.Namespace) -> None:
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
    """
    if pymux.sourcing and pymux.the_client_to_tell() is None:
        # The server, reading a configuration file with nobody
        # attached. #199 is the same shape: a command that raises here
        # takes `source-file` with it, and pymux draws nothing at all.
        return

    target = None
    if args.target_client is not None:
        target = clients_named(pymux, args.target_client)[0]

    set_option(pymux, args, scope=Scope.CLIENT, target=target)


def register(subparsers):
    parser = add_command(subparsers, set_client_option)
    parser.add_argument("-t", dest="target_client", metavar="<target-client>", help="The client of this name, as list-clients prints it.")
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")
