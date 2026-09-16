import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing
from pymux.commands.utils import wrap_argument


def list_keys(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Display all configured key bindings.
    -T: Only this key table.
    """
    # Create help string.
    result = []

    for (table, _keys), custom_binding in (
        pymux.key_bindings_manager.custom_bindings.items()
    ):
        if args.table and args.table != table:
            continue

        result.append(
            "bind-key -T %-16s %s %s"
            % (
                table,
                custom_binding.written,
                " ".join(
                    [custom_binding.command]
                    + list(map(wrap_argument, custom_binding.arguments))
                ),
            )
        )

    show_listing(pymux, "list-keys", "\n".join(sorted(result)))


def register(subparsers):
    parser = add_command(subparsers, list_keys)
    parser.add_argument(
        "-T", dest="table", metavar="<key-table>", help="List only this key table."
    )
