import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.clipboard import ClipboardData
from pymux.commands import add_command


def set_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Put text in a buffer.

    `-b` names one of the named buffers; without it the text lands in
    the session's one buffer, which `paste-buffer` pastes.
    Lillecarl/pymux#303.
    """
    text = args.value or ""
    if args.buffer_name:
        pymux.named_buffers[args.buffer_name] = text
    else:
        pymux.clipboard.set_data(ClipboardData(text))


def register(subparsers):
    parser = add_command(subparsers, set_buffer)
    parser.add_argument("-b", dest="buffer_name", metavar="<buffer-name>", help="The named buffer to fill.")
    parser.add_argument("value", metavar="<value>", nargs="?")
