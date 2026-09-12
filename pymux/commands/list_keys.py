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
    """
    # Create help string.
    result = []

    for k, custom_binding in pymux.key_bindings_manager.custom_bindings.items():
        needs_prefix, _keys = k

        result.append(
            "bind-key %3s %-10s %s %s"
            % (
                ("-n" if needs_prefix else ""),
                custom_binding.written,
                custom_binding.command,
                " ".join(map(wrap_argument, custom_binding.arguments)),
            )
        )

    show_listing(pymux, "list-keys", "\n".join(sorted(result)))


def register(subparsers):
    add_command(subparsers, list_keys)
