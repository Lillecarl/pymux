import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def bind_key(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Bind a key sequence to a command.
    -n: Not necessary to use the prefix.
    """
    key = args.key
    needs_prefix = not args.n

    # The bound command is the first word of the remainder, and its
    # arguments are the rest. A leading `--` separated the two from
    # the options of bind-key on the line; the bound command starts
    # after it.
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    command = arguments[0] if arguments else None
    bound_arguments = arguments[1:]

    try:
        pymux.key_bindings_manager.add_custom_binding(
            key, command, bound_arguments, needs_prefix=needs_prefix
        )
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


def register(subparsers):
    parser = add_command(subparsers, bind_key)
    parser.add_argument("-n", dest="n", action="store_true", help="Bind without the prefix.")
    parser.add_argument("key", metavar="<key>", help="The key to bind.")
    # Everything from the bound command on is a remainder, so an
    # option of the bound command is never read as an option of
    # bind-key; the handler splits it into the command and its
    # arguments. The metavar only says what it is in a usage line.
    parser.add_argument("arguments", nargs=argparse.REMAINDER, metavar="<arguments>")
