import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import get_app
from pymux.commands import CommandException
from pymux.commands import add_command


def show_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Display the content of a buffer.

    `-b` names one of the named buffers; without it the session's one
    buffer shows. Lillecarl/pymux#303.
    """
    name = args.buffer_name
    if name:
        if name not in pymux.named_buffers:
            raise CommandException("Can't find buffer: %s" % (name,))
        text = pymux.named_buffers[name]
    else:
        text = get_app().clipboard.get_data().text
    pymux.get_client_state().layout_manager.display_popup("show-buffer", text)


def register(subparsers):
    parser = add_command(subparsers, show_buffer)
    parser.add_argument("-b", dest="buffer_name", metavar="<buffer-name>", help="The named buffer to show.")
