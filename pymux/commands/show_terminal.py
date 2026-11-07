from __future__ import annotations

import argparse
import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import clients_named, show_listing
from pymux.connection.terminal_answers import readable


def show_terminal(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Show everything a client's terminal said when it was asked.

    TERM and COLORTERM as the client reported them, then every reply
    the terminal gave, in the order it first said each: how often, and
    when last. `-t` names the client; without it this is the client a
    person used last. Lillecarl/pyterm#727.
    """
    if args.target_client is not None:
        client = clients_named(pymux, args.target_client)[0]
    else:
        client = pymux.the_client_to_tell()
    if client is None:
        raise CommandException("no client to show the terminal of")

    lines = [
        "term %s" % (client.client.colors.term or "-",),
        "colorterm %s" % (client.client.colors.colorterm or "-",),
    ]
    for answer in client.client.replies.terminal_said:
        when = datetime.datetime.fromtimestamp(answer.last).strftime("%H:%M:%S")
        lines.append("%s  x%d  %s" % (readable(answer.text), answer.count, when))
    show_listing(pymux, "show-terminal", "\n".join(lines))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_terminal)
    add_target(
        parser,
        "-t",
        kind=Target.CLIENT,
        dest="target_client",
        help="The client of this name, as list-clients prints it.",
    )
