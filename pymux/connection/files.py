"""
The files a client sends from the person's machine for `pymux receive`,
the directories it lists for completing one, and where it saved a
pane's download. Only the client can reach that machine, so the server
asks and waits. Lillecarl/pymux#743, Lillecarl/pymux#745.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from pyte.keep import Keep

from ..pipes.base import BrokenPipeError
from .uploads import LIST_SECONDS, Listing, Upload

if TYPE_CHECKING:
    from .server import ServerConnection

__all__ = ["Files"]


class Files:
    """
    What one client is asked for of the files on its machine, and what it answers.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "connection": Keep.REBUILT,
        # A `pymux receive` that waits ends when the client goes.
        "uploads": Keep.DROPPED,
        "listings": Keep.DROPPED,
    }

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection

        self.uploads: dict[str, Upload] = {}
        self.listings: dict[str, Listing] = {}

    def saved(self, packet: Message) -> None:
        # Where the client saved a pane's download, or why it did not.
        # Only the client knows the path. Lillecarl/pymux#743.
        if self.connection.client_state is not None:
            self.connection.client_state.message = str(packet.get(Field.MESSAGE) or "")
            self.connection.client_state.app.invalidate()

    def took_part(self, packet: Message) -> None:
        # A file `pymux receive` asked this client for, a part at a
        # time. Only the connection that was asked holds the request,
        # so another client cannot answer it. Lillecarl/pymux#743.
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
        `uploads` when it stops waiting. Lillecarl/pymux#743.
        """
        upload = Upload(paths)
        if self.connection.closed:
            upload.end("the client went before it answered")
            return upload
        self.uploads[upload.request] = upload
        self.connection.send_packet(upload.packet(pane))
        return upload

    def stop_waiting(self, upload: Upload) -> None:
        """
        Forget a request whose `pymux receive` stopped waiting. One the
        client has not finished is withdrawn, or its question stays up
        and a later "y" sends a file nobody takes.
        """
        self.uploads.pop(upload.request, None)
        if upload.ended or self.connection.closed:
            return
        with suppress(BrokenPipeError):
            self.connection.send_packet({Field.CMD: Packet.FILES_WITHDRAWN, Field.REQUEST: upload.request})

    async def names_in(self, path: str) -> list[str]:
        """
        The names the client lets show in a directory on the person's
        machine, for completing `pymux receive`. Empty when it answers
        nothing within `LIST_SECONDS`, or goes.
        """
        if self.connection.closed:
            return []
        listed = Listing(path)
        self.listings[listed.request] = listed
        self.connection.send_packet(listed.packet())
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
