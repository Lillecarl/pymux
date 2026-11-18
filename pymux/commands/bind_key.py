from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux import grammar
from pymux.commands import CommandException, CommandParser, add_command, read_commands
from pymux.options.options import a_file_speaks


def bind_key(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Bind a key sequence to a command.
    -n: Not necessary to use the prefix.
    -T: The key table to bind into, a mode's by name.
    -r: For `repeat-time` after the key runs, the next key bound with
        -r works without the prefix: `bind -r H resize-pane -L 5`.
    -N: A note on the key, which `list-keys -N` shows.

    With no command, -N and -r change a binding the key has, and make
    none: tmux's `key_bindings_add`. Lillecarl/pyterm#1033.
    """
    key = args.key
    table = "root" if args.n else (args.table or "prefix")
    manager = pymux.key_bindings_manager

    # The bound command is the first word of the remainder, and its
    # arguments are the rest. A leading `--` separated the two from
    # the options of bind-key on the line; the bound command starts
    # after it.
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if len(arguments) == 1:
        # One word is command text, as tmux reads it: `bind x "a ; b"`,
        # and the `{ }` block that arrives here written back as text.
        try:
            commands = read_commands(pymux, arguments[0])
        except grammar.ParseError as e:
            raise CommandException(e.message)
        arguments = grammar.to_arguments([command.strings() for command in commands])

    try:
        if arguments:
            manager.add_custom_binding(key, arguments[0], arguments[1:], table=table, repeat=args.r, note=args.note)
        else:
            binding = manager.binding_on(key, table)
            if binding is None:
                return
            manager.add_custom_binding(
                key,
                binding.command,
                binding.arguments,
                table=table,
                repeat=binding.repeat or args.r,
                note=binding.note if args.note is None else args.note,
            )
        manager.note_who_bound(key, table, by_hand=not a_file_speaks(pymux))
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, bind_key)
    table = parser.add_mutually_exclusive_group()
    table.add_argument("-n", dest="n", action="store_true", help="Bind without the prefix.")
    table.add_argument(
        "-T",
        dest="table",
        metavar="<key-table>",
        help="Bind into this key table: a mode's name, which the first `bind-key -T` names.",
    )
    parser.add_argument("-r", dest="r", action="store_true", help="The key repeats without the prefix.")
    parser.add_argument("-N", dest="note", metavar="<note>", help="A note on the key, for `list-keys -N`.")
    parser.add_argument("key", metavar="<key>", help="The key to bind.")
    # Everything from the bound command on is a remainder, so an
    # option of the bound command is never read as an option of
    # bind-key; the handler splits it into the command and its
    # arguments. The metavar only says what it is in a usage line.
    parser.add_argument("arguments", nargs=argparse.REMAINDER, metavar="<arguments>")
