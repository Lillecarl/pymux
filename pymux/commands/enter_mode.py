import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def enter_mode(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Enter a mode: from then on its table of keys answers this client.

    A key the table does not name goes to the pane, so the mode is
    transparent unless it binds `Any` itself. The name is the one
    `bind-key -T <name>` binds into. Lillecarl/pymux#394.
    """
    try:
        pymux.key_bindings_manager.enter_mode(args.table)
    except KeyError:
        raise CommandException("no key table named %r" % (args.table,))
    except ValueError:
        raise CommandException("no client attached: a mode belongs to a client")


def register(subparsers):
    parser = add_command(subparsers, enter_mode)
    parser.add_argument("table", metavar="<key-table>", help="The mode to enter.")
