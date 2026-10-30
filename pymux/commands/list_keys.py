from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import show_listing
from pymux.commands.utils import wrap_argument


def list_keys(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Display all configured key bindings.
    -T: Only this key table.
    """
    # Create help string.
    result = []

    for (table, _keys), custom_binding in pymux.key_bindings_manager.custom_bindings.items():
        if args.table and args.table != table:
            continue

        result.append(
            "bind-key -T %-16s %s %s"
            % (
                table,
                custom_binding.written,
                " ".join([custom_binding.command, *list(map(wrap_argument, custom_binding.arguments))]),
            )
        )

    # The prefix pressed twice is built in, so that it follows the
    # prefix option, and is listed as the binding it acts as.
    manager = pymux.key_bindings_manager
    if args.table in (None, "prefix") and manager.prefix_sends_itself():
        result.append("bind-key -T %-16s %s send-prefix" % ("prefix", manager.prefix_written))

    show_listing(pymux, "list-keys", "\n".join(sorted(result)))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, list_keys)
    parser.add_argument("-T", dest="table", metavar="<key-table>", help="List only this key table.")
