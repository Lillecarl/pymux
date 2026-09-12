import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def unbind_key(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Remove a key binding.
    """
    key = args.key
    needs_prefix = not args.n

    try:
        pymux.key_bindings_manager.remove_custom_binding(key, needs_prefix=needs_prefix)
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


def register(subparsers):
    parser = add_command(subparsers, unbind_key)
    parser.add_argument("-n", dest="n", action="store_true", help="Remove a binding that needs no prefix.")
    parser.add_argument("key", metavar="<key>")
