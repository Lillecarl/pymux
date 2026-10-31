"""
A client that runs one command over a unix socket and prints the answer.

`send-keys` from a script, a completion on Tab, a pane's browser shim:
none of them draws, so none of them imports what draws.
`tests/test_entry_imports.py` holds that. Lillecarl/pymux#589.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
from typing import override

from libpymux.agentic import caller_fields
from libpymux.protocol import READ_SIZE, Field, Framer, Message, Packet
from libpymux.wire import encode_json

from .base import Client

__all__ = ["NO_ANSWER", "CommandClient"]

#: A server ends every command with its exit code. A peer that closes
#: without one ran no command, or is not a pymux server. Lillecarl/pymux#606.
NO_ANSWER = "pymux: %s closed the connection with no answer. Is a pymux server listening there?\n"


class CommandClient(Client):
    "One command to the server on a unix socket, and its answer."

    def __init__(self, socket_name: str) -> None:
        self.socket_name = socket_name
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(socket_name)
        self.socket.setblocking(True)

    @override
    def run_command(self, command: str, pane_id: str | None = None, timeout: float | None = None) -> int:
        """
        Ask the server to run this command. Print the output that the server
        sends back, and return the exit code of the command.

        :param pane_id: Optional identifier of the current pane.
        :param timeout: How long the answer may take, in seconds, when a
            number is given. A server always closes the connection after
            the answer, so the read ends with it; the number is for a
            peer that never answers at all -- something else listens on
            the path. `None`, the default, waits the way the attach
            always has.
        """
        self.send_packet(
            {
                Field.CMD: Packet.RUN_COMMAND,
                Field.DATA: command,
                Field.PANE_ID: pane_id,
                # Who and from where. The server cannot read the
                # caller's context itself, so the client sends the
                # whole environment -- resolved to one id under
                # `PYMUX_AGENTIC_ID` beside it -- and the directory it
                # stands in. `libpymux/agentic.py` holds the names.
                **caller_fields(),
            }
        )
        if timeout is not None:
            # After the send, which asked for blocking again: the read
            # is the side that needs the patience.
            self.socket.settimeout(timeout)

        exit_code = None
        framer = Framer()

        while True:
            try:
                data = self.socket.recv(READ_SIZE)
            except OSError:
                break

            if not data:
                break  # Connection closed.

            for packet_data in framer.feed(data):
                packet = json.loads(packet_data.decode("utf-8"))

                if packet[Field.CMD] == Packet.OUT:
                    sys.stdout.write(packet[Field.DATA])
                    sys.stdout.flush()
                elif packet[Field.CMD] == Packet.ERR:
                    sys.stderr.write(packet[Field.DATA])
                    sys.stderr.flush()
                elif packet[Field.CMD] == Packet.EXIT:
                    exit_code = packet[Field.CODE]

        if exit_code is None:
            sys.stderr.write(NO_ANSWER % (self.socket_name,))
            return 1
        return exit_code

    def send_packet(self, data: Message) -> None:
        "Send to the server, and say nothing when it has gone: the read says that."
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.socket.sendall(encode_json(data))
