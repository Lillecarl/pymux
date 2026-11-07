"""
`pymux receive` for a person who attaches through a peer.

The owner runs the command and writes each file, as it does for a client
of its own. Only the peer reaches the person's client, so the owner asks
the peer, the peer asks its client, and each answer travels back whole.
The client decides what it sends, as it always does: the peer only
passes packets on. Lillecarl/pyterm#949.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

import anyio
from libpymux.protocol import Field, Message, Packet

from ..connection.files import why_it_sends_no_file
from ..connection.uploads import LIST_SECONDS, Listing, Upload, send_files_packet

if TYPE_CHECKING:
    from ..connection.files import Files
    from ..core.main import Pymux
    from ..core.pane import Pane

__all__ = ["LinkFiles", "PeerSender", "Relay", "Relays"]


class LinkFiles:
    "The owner's side: what `pymux receive` waits on from one peer."

    def __init__(self, queue: Callable[[Message], None]) -> None:
        self.queue = queue
        self.uploads: dict[str, Upload] = {}
        self.listings: dict[str, Listing] = {}

    def sender(self, pane: Pane) -> PeerSender:
        return PeerSender(self, pane)

    def took_part(self, packet: Message) -> None:
        "A `peer-upload`: one packet of the peer's client, whole."
        inner = packet.get(Field.DATA)
        if not isinstance(inner, Mapping):
            return
        upload = self.uploads.get(str(inner.get(Field.REQUEST)))
        if upload is not None:
            upload.take(inner)

    def listed(self, packet: Message) -> None:
        listed = self.listings.pop(str(packet.get(Field.REQUEST)), None)
        if listed is not None:
            listed.take(packet)

    def gone(self) -> None:
        "The link ended: a `pymux receive` waiting on it would wait for ever."
        for upload in self.uploads.values():
            upload.end("the peer went before its client answered")
        self.uploads.clear()
        for listed in self.listings.values():
            listed.answered.set()
        self.listings.clear()


class PeerSender:
    "What `pymux receive` in `pane` asks, when the person sits at a peer: `Files`, over the link."

    def __init__(self, files: LinkFiles, pane: Pane) -> None:
        self.files = files
        self.pane = pane

    def ask_for(self, paths: list[str], pane: str) -> Upload:
        upload = Upload(paths)
        self.files.uploads[upload.request] = upload
        self.files.queue(
            {
                Field.CMD: Packet.MIRROR_SEND_FILES,
                Field.REQUEST: upload.request,
                Field.PATHS: paths,
                Field.PANE_ID: int(self.pane.pane_id),
            }
        )
        return upload

    def stop_waiting(self, upload: Upload) -> None:
        self.files.uploads.pop(upload.request, None)
        if not upload.ended:
            self.files.queue({Field.CMD: Packet.MIRROR_FILES_WITHDRAWN, Field.REQUEST: upload.request})

    async def names_in(self, path: str) -> list[str]:
        listed = Listing(path)
        self.files.listings[listed.request] = listed
        self.files.queue(
            {
                Field.CMD: Packet.MIRROR_LIST_FILES,
                Field.REQUEST: listed.request,
                Field.PATH: path,
                Field.PANE_ID: int(self.pane.pane_id),
            }
        )
        # The peer waits `LIST_SECONDS` on its client, and the link adds a round trip.
        with anyio.move_on_after(2 * LIST_SECONDS):
            await listed.answered.wait()
        self.files.listings.pop(listed.request, None)
        return listed.names


class Relay:
    """
    The peer's side of one request: it sits in its client's `Files` as an
    `Upload` would, and sends each packet the client answers to the owner.
    """

    def __init__(self, request: str, paths: list[str], send: Callable[[Message], None]) -> None:
        self.request = request
        self.paths = paths
        self.send = send
        self.ended = False
        #: Called once, when the client said all it will.
        self.on_end: Callable[[], None] = lambda: None

    def packet(self, pane: str) -> Message:
        return send_files_packet(self.request, self.paths, pane)

    def take(self, packet: Mapping[str, object]) -> None:
        if self.ended:
            return
        self.send({Field.CMD: Packet.PEER_UPLOAD, Field.DATA: dict(packet)})
        if packet.get(Field.CMD) == Packet.UPLOAD_DONE:
            self._ended()

    def end(self, why: str | None) -> None:
        if self.ended:
            return
        self.take({Field.CMD: Packet.UPLOAD_DONE, Field.REQUEST: self.request, Field.ERROR: why})

    def _ended(self) -> None:
        self.ended = True
        self.on_end()


class Relays:
    "The peer's side: the requests its clients answer for the owner."

    def __init__(self, pymux: Pymux, send: Callable[[Message], None]) -> None:
        self.pymux = pymux
        self.send = send
        self.held: dict[str, tuple[Files, Relay]] = {}

    def send_files(self, pane: Pane | None, packet: Message) -> None:
        request = str(packet.get(Field.REQUEST) or "")
        paths = packet.get(Field.PATHS)
        relay = Relay(request, [str(path) for path in paths] if isinstance(paths, list) else [], self.send)
        client = self.pymux.outward.download_client(pane) if pane is not None else None
        refused = "the pane is not mirrored here" if pane is None else why_it_sends_no_file(client)
        if refused is not None or pane is None or client is None:
            relay.end(refused)
            return
        files = client.client.files
        self.held[request] = (files, relay)

        def ended() -> None:
            self.held.pop(request, None)
            files.uploads.pop(request, None)

        relay.on_end = ended
        files.ask_for_a_peer(relay, "%%%d" % (pane.pane_id,))

    def withdrawn(self, packet: Message) -> None:
        held = self.held.pop(str(packet.get(Field.REQUEST) or ""), None)
        if held is not None:
            files, relay = held
            files.stop_waiting(relay)

    async def list_files(self, pane: Pane | None, packet: Message) -> None:
        client = self.pymux.outward.download_client(pane) if pane is not None else None
        names: list[str] = []
        if why_it_sends_no_file(client) is None and client is not None:
            names = await client.client.files.names_in(str(packet.get(Field.PATH) or "~"))
        self.send({Field.CMD: Packet.PEER_FILES_LISTED, Field.REQUEST: packet.get(Field.REQUEST), Field.PATHS: names})

    def gone(self) -> None:
        "The link to the owner ended: withdraw what the clients here were asked."
        for files, relay in list(self.held.values()):
            files.stop_waiting(relay)
        self.held.clear()
