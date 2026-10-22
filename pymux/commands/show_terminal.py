from __future__ import annotations

import argparse
import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import clients_named, show_listing
from pymux.terminal_answers import readable


def show_terminal(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Show everything a client's terminal said when it was asked.

    TERM and COLORTERM as the client reported them, then every reply
    the terminal gave, in the order it first said each: how often, and
    when last. `-t` names the client; without it this is the client a
    person used last. Lillecarl/pymux#725.
    """
    if args.target_client is not None:
        client = clients_named(pymux, args.target_client)[0]
    else:
        client = pymux.the_client_to_tell()
    if client is None:
        raise CommandException("no client to show the terminal of")

    connection = client.connection
    said = getattr(connection, "terminal_said", None)
    colors = getattr(connection, "colors", None)
    lines = [
        "term %s" % (getattr(colors, "term", "") or "-",),
        "colorterm %s" % (getattr(colors, "colorterm", "") or "-",),
    ]
    for answer in said or ():
        when = datetime.datetime.fromtimestamp(answer.last).strftime("%H:%M:%S")
        lines.append("%s  x%d  %s" % (readable(answer.text), answer.count, when))
    show_listing(pymux, "show-terminal", "\n".join(lines))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_terminal)
    parser.add_argument(
        "-t",
        dest="target_client",
        metavar="<target-client>",
        help="The client of this name, as list-clients prints it.",
    )
