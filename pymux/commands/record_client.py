from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_client
from pymux.commands.record_pane import seconds
from pymux.recording import Recording, a_name, recordings_directory

if TYPE_CHECKING:
    from pymux.main import Pymux


def record_client(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Start recording what pymux draws on a client's terminal, or stop.

    It keeps every frame that client got, the keys it sent and each
    resize, so a replay shows what the person saw: every pane, the
    borders, the status line, whatever floats. The files are the three
    `record-pane` writes, in the same place, and `record-export` turns
    either into a web replay or an asciicast. Lillecarl/pymux#612.

    The recording starts with the whole screen drawn again, so it does
    not begin as changes to a screen it never held. It ends when the
    client detaches.
    """
    client_state = the_client(pymux, args.target_client, "record-client")
    connection = client_state.connection
    if connection.recording is not None:
        recording, connection.recording = connection.recording, None
        try:
            written = recording.finish()
        except OSError as e:
            raise CommandException("record-client: %s" % e)
        pymux.show_message("Recorded %s" % written)
    else:
        size = connection.size
        connection.recording = Recording(
            args.name or a_name("client"),
            size.rows,
            size.columns,
            connection.colors.term,
            args.idle_time_limit,
        )
        pymux.waking.redraw(client_state)
        pymux.show_message("Recording %s to %s" % (connection.recording.name, recordings_directory()))
    pymux.invalidate()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, record_client)
    parser.add_argument("-t", dest="target_client", metavar="<target-client>", help="The client to record.")
    parser.add_argument(
        "-i", dest="idle_time_limit", type=seconds, metavar="<seconds>", help="The longest pause a player shows."
    )
    parser.add_argument("name", nargs="?", metavar="<name>", help="What the files are called.")
