import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def open_url(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Open a URL in the browser of a client.

    The client that gets it follows `open-url-target`, and whether it
    asks first follows `open-url-mode`. "-c" is the answer of a
    confirmation: it opens without asking again.
    """
    pymux.open_url(args.url, confirmed=bool(args.c))


def register(subparsers):
    parser = add_command(subparsers, open_url)
    parser.add_argument("-c", dest="c", action="store_true", help="Open without asking again.")
    parser.add_argument("url", metavar="<url>")
