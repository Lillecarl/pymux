"""
The files a client sends from the person's machine for `pymux receive`,
the directories it lists for completing one, and where it saved a
pane's download. Only the client can reach that machine, so the server
asks and waits. Lillecarl/pyterm#745, Lillecarl/pyterm#747.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from pyte.keep import Keep

from ..enums import FileTransfer
from ..pipes.base import BrokenPipeError
from .uploads import LIST_SECONDS, Listing, Upload

if TYPE_CHECKING:
    from ..mirror.files import Relay
    from .client import ClientRole
    from .client_state import ClientState

__all__ = ["Files", "why_it_sends_no_file"]


def why_it_sends_no_file(client: ClientState | None) -> str | None:
    "Why `client` cannot send a file for `pymux receive`, or None when it can."
    if client is None:
        return "Nobody is attached to the session of this pane, so nobody can send a file."
    if client.options.file_transfer is FileTransfer.PASSTHROUGH:
        return "file-transfer is passthrough, and only the pymux client sends a file."
    if not client.client.sends_files:
        return "This pymux client is too old to send a file; start a new one."
    return None


class Files:
    """
    What one client is asked for of the files on its machine, and what it answers.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "client": Keep.REBUILT,
        # A `pymux receive` that waits ends when the client goes.
        "uploads": Keep.DROPPED,
        "listings": Keep.DROPPED,
    }

    def __init__(self, client: ClientRole) -> None:
        self.client = client

        #: An `Upload` the command here waits on, or a peer's `Relay`.
        self.uploads: dict[str, Upload | Relay] = {}
        self.listings: dict[str, Listing] = {}

    def saved(self, packet: Message) -> None:
        # Where the client saved a pane's download, or why it did not.
        # Only the client knows the path. Lillecarl/pyterm#745.
        if self.client.state is not None:
            self.client.state.message = str(packet.get(Field.MESSAGE) or "")
            self.client.state.app.invalidate()

    def took_part(self, packet: Message) -> None:
        # A file `pymux receive` asked this client for, a part at a
        # time. Only the connection that was asked holds the request,
        # so another client cannot answer it. Lillecarl/pyterm#745.
        upload = self.uploads.get(str(packet.get(Field.REQUEST)))
        if upload is not None:
            upload.take(packet)

    def listed(self, packet: Message) -> None:
        listed = self.listings.pop(str(packet.get(Field.REQUEST)), None)
        if listed is not None:
            listed.take(packet)

    def ask_for(self, paths: list[str], pane: str) -> Upload:
        """
        Ask this client for files on the person's machine, for
        `pymux receive` in `pane`. The caller removes the request from
        `uploads` when it stops waiting. Lillecarl/pyterm#745.
        """
        upload = Upload(paths)
        if self.client.connection.closed:
            upload.end("the client went before it answered")
            return upload
        self.uploads[upload.request] = upload
        self.client.connection.send_packet(upload.packet(pane))
        return upload

    def ask_for_a_peer(self, relay: Relay, pane: str) -> None:
        "Ask this client for the files an owner's `pymux receive` wants, through `relay`. Lillecarl/pyterm#949."
        if self.client.connection.closed:
            relay.end("the client went before it answered")
            return
        self.uploads[relay.request] = relay
        self.client.connection.send_packet(relay.packet(pane))

    def stop_waiting(self, upload: Upload | Relay) -> None:
        """
        Forget a request whose `pymux receive` stopped waiting. One the
        client has not finished is withdrawn, or its question stays up
        and a later "y" sends a file nobody takes.
        """
        self.uploads.pop(upload.request, None)
        if upload.ended or self.client.connection.closed:
            return
        with suppress(BrokenPipeError):
            self.client.connection.send_packet({Field.CMD: Packet.FILES_WITHDRAWN, Field.REQUEST: upload.request})

    async def names_in(self, path: str) -> list[str]:
        """
        The names the client lets show in a directory on the person's
        machine, for completing `pymux receive`. Empty when it answers
        nothing within `LIST_SECONDS`, or goes.
        """
        if self.client.connection.closed:
            return []
        listed = Listing(path)
        self.listings[listed.request] = listed
        self.client.connection.send_packet(listed.packet())
        with anyio.move_on_after(LIST_SECONDS):
            await listed.answered.wait()
        self.listings.pop(listed.request, None)
        return listed.names

    def client_went(self) -> None:
        "End every request: a `pymux receive` waiting for this client would wait for ever."
        for upload in self.uploads.values():
            upload.end("the client went before it answered")
        self.uploads.clear()
        for listed in self.listings.values():
            listed.answered.set()
        self.listings.clear()
