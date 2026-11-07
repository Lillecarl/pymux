from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from libpymux.protocol import Capability

from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import the_client
from pymux.commands.record_pane import seconds
from pymux.connection.recording import Recording, a_name, recordings_directory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pymux.core.main import Pymux
    from pymux.mirror.peer import Peer


def record_client(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Start recording what pymux draws on a client's terminal, or stop.

    It keeps every frame that client got, the keys it sent and each
    resize, so a replay shows what the person saw: every pane, the
    borders, the status line, whatever floats. The files are the three
    `record-pane` writes, in the same place, and `record-export` turns
    either into a web replay or an asciicast. Lillecarl/pyterm#614.

    The recording starts with the whole screen drawn again, so it does
    not begin as changes to a screen it never held. It ends when the
    client detaches.

    **A client on a mirrored session records to the session's owner**:
    the work is there, so the files go there too, over the peer link,
    and land in its recordings directory. `-l` keeps them here, and so
    does a link that is down or too old when the recording ends.
    Lillecarl/pyterm#957.
    """
    client_state = the_client(pymux, args.target_client, "record-client")
    client = client_state.client
    if client.recording is not None:
        recording, client.recording = client.recording, None
        try:
            written = recording.finish()
        except OSError as e:
            raise CommandException("record-client: %s" % e)
        pymux.show_message("Recorded %s" % written)
    else:
        size = client.connection.size
        client.recording = Recording(
            args.name or a_name("client"),
            size.rows,
            size.columns,
            client.colors.term,
            args.idle_time_limit,
        )
        remote = client_state.session.remote
        if remote is not None and not args.local:
            client.recording.forward = _to_the_owner(remote.peer)
            where = "the recordings of %s" % (remote.peer.server or remote.peer.address,)
        else:
            where = str(recordings_directory())
        pymux.waking.redraw(client_state)
        pymux.show_message("Recording %s to %s" % (client.recording.name, where))
    pymux.invalidate()


def _to_the_owner(peer: Peer) -> Callable[[list[Path]], str | None]:
    "What sends a finished recording's files to `peer`'s owner, or None while it cannot take them."

    def forward(files: list[Path]) -> str | None:
        if not peer.connected or not peer.can(Capability.RECORDINGS):
            return None
        peer.send_recording(files)
        recorded = next((one.name for one in files if one.suffix == ".bin"), files[0].name)
        return "%s on %s" % (recorded, peer.server or peer.address)

    return forward


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, record_client)
    add_target(parser, "-t", kind=Target.CLIENT, dest="target_client", help="The client to record.")
    parser.add_argument(
        "-i", dest="idle_time_limit", type=seconds, metavar="<seconds>", help="The longest pause a player shows."
    )
    parser.add_argument(
        "-l", dest="local", action="store_true", help="Keep the files on this server, even on a mirrored session."
    )
    parser.add_argument("name", nargs="?", metavar="<name>", help="What the files are called.")
