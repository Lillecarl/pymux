"""
The peer's side of a peer link: this server, holding mirrors of panes
another server owns.

One link per server, whatever number of its panes are mirrored here.
`link.py` is the other end.
"""

from __future__ import annotations

import math
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar, cast

import anyio
from libpymux.connection import CommandResult, Reply, command_packet
from libpymux.protocol import Field, Message, Packet
from libpymux.wire import MSGPACK, MSGPACK_ZSTD, PacketReader, encode_json
from pyte.freeze import freeze_value
from pyte.keep import Keep

from ..client.defaults import is_ssh_url
from ..commands import CommandException
from ..log import logger
from .dial import dial, dial_beside
from .program import RemoteProgram
from .tree import MirrorTree

if TYPE_CHECKING:
    from prompt_toolkit.data_structures import Size

    from ..core.main import Pymux
    from ..core.window_tree import Window
    from ..terminals import PeerTerminal
    from .sink import MirrorSink

__all__ = ["Peer", "Peers"]


class Peers:
    "This server's links to other servers, one per address."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        "by_address": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.by_address: dict[str, Peer] = {}

    def of(self, address: str, **connect_with: Any) -> Peer:
        "The link to `address`, opened in the server's scope if there is none yet."
        peer = self.by_address.get(address)
        if peer is None:
            if self.pymux.tasks is None:
                raise RuntimeError("A peer link was asked for outside `Pymux.running`, so nothing would hold it.")
            peer = self.by_address[address] = Peer(self.pymux, address, **connect_with)
            peer.tell_terminal(self.pymux.terminals.peer_terminal())
            self.pymux.tasks.start_soon(peer.run)
        return peer

    def sync_sight(self) -> None:
        """
        Tell every mirrored pane whether a client here shows it: the
        panes of the window each client looks at, in a mirrored session.
        And tell each owner how much room those windows have here, so
        it lays them out at that size. `Pymux.invalidate` calls this,
        since a switch, a split and a resize all reach it.
        """
        if not self.by_address:
            return
        seen: set[int] = set()
        windows: list[Window] = []
        for client_state in self.pymux._client_states.values():
            session = client_state.session
            if client_state.temporary or session.remote is None:
                continue
            window = session.arrangement.get_active_window_for(client_state.app)
            if window is not None:
                seen.update(id(pane) for pane in window.panes)
                windows.append(window)
        for peer in self.by_address.values():
            for pane in peer.tree.panes.values():
                cast(RemoteProgram, pane.process).seen(id(pane) in seen)
            peer.tell_sizes(
                {
                    remote_id: self.pymux.planes.size(window)
                    for remote_id, window in peer.tree.windows.items()
                    if any(window is one for one in windows)
                }
            )


class Peer:
    """
    A link to the server at `address`, and the mirrors on it.

    What this side sends goes into one queue that a writer drains, so
    a packet sent before the link is up waits and goes first: the hello,
    then each mirror asked for meanwhile.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        # A mirror is rebuilt by its program after an upgrade, and asks
        # the next link for a fresh freeze.
        "address": Keep.REBUILT,
        "connect_with": Keep.REBUILT,
        "sinks": Keep.DROPPED,
        "connected": Keep.DROPPED,
        "error": Keep.DROPPED,
        "_send": Keep.DROPPED,
        "_receive": Keep.DROPPED,
        "_terminal_told": Keep.DROPPED,
        "_sizes_told": Keep.DROPPED,
        "_stream": Keep.DROPPED,
        "tree": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux, address: str, **connect_with: Any) -> None:
        self.address = address
        self.connect_with = connect_with
        self.sinks: dict[int, MirrorSink] = {}
        #: The owner's sessions as built here, once this side watches.
        self.tree = MirrorTree(pymux, self)
        #: Whether the link is up.
        self.connected = False
        #: Why the link went or never came, for the person to read.
        self.error: str | None = None
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)
        self._terminal_told: PeerTerminal | None = None
        self._sizes_told: dict[int, Size] = {}
        self._stream: anyio.abc.ByteStream | None = None
        # Zstd only across a network, as a client offers it.
        offer = [MSGPACK_ZSTD, MSGPACK] if is_ssh_url(address) else [MSGPACK]
        self.send({Field.CMD: Packet.PEER, Field.WIRE: offer})

    def send(self, packet: Message) -> None:
        self._send.send_nowait(encode_json(packet))

    # -- what a mirror asks --------------------------------------------

    def mirror(self, pane_id: int, sink: MirrorSink) -> None:
        self.sinks[pane_id] = sink
        self.send({Field.CMD: Packet.MIRROR, Field.PANE_ID: pane_id})

    def stop(self, pane_id: int) -> None:
        if self.sinks.pop(pane_id, None) is not None:
            self.send({Field.CMD: Packet.MIRROR_STOP, Field.PANE_ID: pane_id})

    def pause(self, pane_id: int) -> None:
        self.send({Field.CMD: Packet.MIRROR_PAUSE, Field.PANE_ID: pane_id})

    def resume(self, pane_id: int) -> None:
        self.send({Field.CMD: Packet.MIRROR_RESUME, Field.PANE_ID: pane_id})

    def type(self, pane_id: int, data: str) -> None:
        self.send({Field.CMD: Packet.MIRROR_INPUT, Field.PANE_ID: pane_id, Field.DATA: data})

    def tell_sizes(self, sizes: dict[int, Size]) -> None:
        "Tell the owner the room each of its windows has here, when that changed."
        if sizes == self._sizes_told:
            return
        self._sizes_told = sizes
        self.send(
            {
                Field.CMD: Packet.PEER_SIZES,
                Field.SIZES: [[window_id, size.rows, size.columns] for window_id, size in sorted(sizes.items())],
            }
        )

    def watch(self) -> None:
        "Ask the owner for its sessions, now and on every change."
        self.send({Field.CMD: Packet.WATCH})

    def focus(self, pane_id: int, focused: bool) -> None:
        self.send({Field.CMD: Packet.MIRROR_FOCUS, Field.PANE_ID: pane_id, Field.FOCUSED: focused})

    def tell_terminal(self, terminal: PeerTerminal) -> None:
        "Tell the owner what the terminals here say, when it changed (`Terminals.tell_peers`)."
        if terminal == self._terminal_told:
            return
        self._terminal_told = terminal
        self.send(
            {
                Field.CMD: Packet.PEER_TERMINAL,
                Field.CELL_SIZE: None if terminal.cell_size is None else list(terminal.cell_size),
                Field.COLOR_BASE: None if terminal.color_base is None else freeze_value(terminal.color_base),
                Field.KEYBOARD: terminal.keyboard_source_flags,
                Field.THEME: terminal.theme,
            }
        )

    async def run_command(self, text: str, pane_id: int | None, targets: dict[str, str]) -> CommandResult:
        """
        Run one command on the owner, as a command typed in its pane
        `pane_id` runs there, and read what it says. It goes on a
        stream of its own, because the owner answers a command and
        closes the connection it came on.
        """
        if self._stream is None:
            raise CommandException("the link to %s is down" % (self.address,))
        reply = Reply()
        async with await dial_beside(self.address, self._stream) as stream:
            await stream.send(
                command_packet(text, None if pane_id is None else str(pane_id), None, targets, forwarded=True)
            )
            while True:
                try:
                    data = await stream.receive()
                except anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError:
                    break
                reply.feed(data)
        return reply.result()

    # -- the link ------------------------------------------------------

    async def run(self) -> None:
        """
        Hold the link until it goes. A mirror keeps the last screen it
        was sent when the link goes.
        """
        try:
            stream = await dial(self.address, **self.connect_with)
        except Exception as error:
            self.error = str(error) or type(error).__name__
            logger.warning("Could not reach the server at %s: %s", self.address, self.error)
            return
        self.connected = True
        self._stream = stream
        # This runs in the server's own scope: what it raises would end
        # every client, so the link's trouble stays the link's.
        try:
            async with stream, anyio.create_task_group() as tasks:
                tasks.start_soon(self._write, stream)
                reader = PacketReader()
                while True:
                    try:
                        data = await stream.receive()
                    except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError) as error:
                        self.error = str(error) or "the server closed the link"
                        break
                    for packet in reader.feed(data):
                        self._take(packet)
                tasks.cancel_scope.cancel()
        except Exception as error:
            self.error = str(error) or type(error).__name__
            logger.exception("The link to %s failed.", self.address)
        finally:
            self.connected = False
            self._stream = None
            logger.info("The link to %s went: %s", self.address, self.error)

    async def _write(self, stream: anyio.abc.ByteStream) -> None:
        # A send that fails ends the link the read loop is holding.
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            async for encoded in self._receive:
                await stream.send(encoded)
        await stream.aclose()

    def _take(self, packet: Message) -> None:
        if packet.get(Field.CMD) == Packet.TREE:
            try:
                self.tree.update(str(packet.get(Field.SERVER) or self.address), packet.get(Field.ROWS) or {})
            except Exception:
                # A tree this side cannot build leaves the last one standing.
                logger.exception("The sessions of %s could not be mirrored.", self.address)
            return
        pane_id = packet.get(Field.PANE_ID)
        sink = self.sinks.get(pane_id) if isinstance(pane_id, int) else None
        if sink is None:
            return
        try:
            sink.take(packet)
        except Exception:
            # One mirror that cannot take what it was sent must not end
            # the link for the others.
            logger.exception("Mirror of pane %s on %s failed a packet.", pane_id, self.address)
