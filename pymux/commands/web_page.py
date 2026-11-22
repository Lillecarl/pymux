"""
`web-page`: tell a pymux web page to act. Lillecarl/pyterm#1076.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from libpymux.protocol import Field, Packet, PageAction

from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import clients_named

if TYPE_CHECKING:
    from pymux.core.main import Pymux


def web_page(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Tell each `pymux web` page to reload or to ask for notifications.

    Each page of a session view acts, or only the page of the client
    `-t` names. `reload` loads the page again, as a person would after a deploy.
    `notifications` asks the person to allow notifications: it shows the
    page's 🔔 and a line about it, because a browser asks only after a
    person's tap.
    """
    pages = [
        one
        for one in (clients_named(pymux, args.target_client) if args.target_client is not None else pymux.clients)
        if one.client.web_page
    ]
    if not pages:
        raise CommandException("no web page is attached" + ("" if args.target_client is None else " as that client"))
    for client_state in pages:
        client_state.client.connection.send_packet({Field.CMD: Packet.PAGE, Field.DATA: args.action})


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, web_page)
    add_target(
        parser,
        "-t",
        kind=Target.CLIENT,
        dest="target_client",
        help="The web client of this name, as list-clients prints it.",
    )
    parser.add_argument(
        "action", choices=[action.value for action in PageAction], metavar="<action>", help="reload or notifications."
    )
