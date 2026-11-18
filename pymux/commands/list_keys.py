from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux
    from pymux.keys.key_bindings import CustomBinding


from pymux.commands import CommandParser, add_command
from pymux.commands.common import show_listing
from pymux.grammar import print_arguments


def list_keys(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Display all configured key bindings.
    -T: Only this key table.
    -N: The root and prefix keys that carry a note, each after the
        prefix with its note, as tmux lists them.
    -a: With -N, the keys without a note too, with their command.
    """
    manager = pymux.key_bindings_manager
    bindings = [
        (table, binding)
        for (table, _keys), binding in manager.custom_bindings.items()
        if args.table in (None, table) and (not args.N or args.table or table in ("root", "prefix"))
    ]

    def command_of(binding: CustomBinding) -> str:
        return print_arguments([binding.command, *binding.arguments])

    result = []
    if args.N:
        for _table, binding in bindings:
            if binding.note is not None or args.a:
                note = binding.note if binding.note is not None else command_of(binding)
                result.append("%s %s %s" % (manager.prefix_written, binding.written, note))
    else:
        repeat_column = any(binding.repeat for _table, binding in bindings)
        for table, binding in bindings:
            flag = ("-r " if binding.repeat else "   ") if repeat_column else ""
            result.append("bind-key %s-T %-16s %s %s" % (flag, table, binding.written, command_of(binding)))

        # The prefix pressed twice is built in, so that it follows the
        # prefix option, and is listed as the binding it acts as.
        if args.table in (None, "prefix") and manager.prefix_sends_itself():
            flag = "   " if repeat_column else ""
            result.append("bind-key %s-T %-16s %s send-prefix" % (flag, "prefix", manager.prefix_written))

    show_listing(pymux, "list-keys", "\n".join(sorted(result)))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, list_keys)
    parser.add_argument("-T", dest="table", metavar="<key-table>", help="List only this key table.")
    parser.add_argument("-N", dest="N", action="store_true", help="The keys with a note, and the note.")
    parser.add_argument("-a", dest="a", action="store_true", help="With -N, the keys without a note too.")
