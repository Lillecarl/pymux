import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def unbind_key(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Remove a key binding.
    -n: Remove a binding that needs no prefix.
    -T: The key table to remove it from, a mode's by name.
    """
    key = args.key
    table = "root" if args.n else (args.table or "prefix")

    try:
        pymux.key_bindings_manager.remove_custom_binding(key, table=table)
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


def register(subparsers):
    parser = add_command(subparsers, unbind_key)
    table = parser.add_mutually_exclusive_group()
    table.add_argument("-n", dest="n", action="store_true", help="Remove a binding that needs no prefix.")
    table.add_argument(
        "-T", dest="table", metavar="<key-table>", help="Remove it from this key table."
    )
    parser.add_argument("key", metavar="<key>")
