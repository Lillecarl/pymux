from __future__ import annotations

import socket
from collections.abc import Coroutine
from contextlib import suppress
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
)

import anyio
from libpymux.protocol import OLDEST_PEER_VERSION, PEER_VERSION, Field, Message, Packet, peer_upgrade, peer_version
from libpymux.wire import encode_json
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from pyte.keep import Keep

from ..enums import Woke
from ..graphics.graphics import CELL_SIZE_QUERY
from ..log import logger
from ..mirror.link import PeerLink
from ..mirror.tunnel import OwnerTunnel
from ..pipes.base import BrokenPipeError
from .attach import start_gui
from .packet_table import PacketTable
from .rate import Rate
from .requests import Requests

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pymux.core.main import Pymux
    from pymux.pipes.base import PipeConnection

    from .client import ClientRole

__all__ = ["Role", "ServerConnection"]

#: How many reads in a row may fail before a connection is closed.
#: A packet that cannot be read is answered and the next one is taken,
#: which is what makes one bad packet harmless; a read that fails again
#: and again is a connection nothing can use. Lillecarl/pymux#329.
FAILURES_THAT_END_A_CONNECTION = 5

#: How often to ask the far end of a connection whether it is still
#: there, in seconds, and how many unanswered pings end it.
#:
#: Thirty seconds of a ghost, and one small packet per fifteen seconds
#: per attached client or peer link. It is a backstop and not the first line: over
#: SSH the client notices its own machine woke (Lillecarl/pymux#445)
#: and a server's sshd may reap the session too. This is what covers
#: the cases neither does -- a client process that was stopped, or a
#: transport with no liveness of its own. Lillecarl/pymux#446.
PING_INTERVAL = 15.0
PING_MISSES = 2

#: The packets a connection takes itself whatever its role.
_TRANSPORT_PACKETS = frozenset({Packet.WIRE, Packet.PONG})


class Role(Protocol):
    """
    What a connection is for: an attached client (`ClientRole`), a peer
    link (`PeerLink`) or a tunnel (`OwnerTunnel`). The first packet that
    chooses it is `start-gui`, `run-command`, `peer` or `tunnel`, and
    from then on every packet but the transport's own goes to it.
    Lillecarl/pymux#882, Lillecarl/pymux#886.
    """

    def handle(self, packet: Message) -> None: ...


#: The method of `ServerConnection` that handles each kind of packet.
_PACKETS: PacketTable[ServerConnection] = PacketTable()


class ServerConnection:
    """
    For each client that connects, we have one instance of this class.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: The socket ends with the old server and the client reconnects
    #: (Lillecarl/pymux#409), so the attach handshake rebuilds most of
    #: this. Saved: the client's own record and when it came.
    KEEP: ClassVar[dict[str, Keep]] = {
        # `ClientRole.KEEP` says what of it.
        "client": Keep.SAVED,
        "pymux": Keep.REBUILT,
        "requests": Keep.REBUILT,
        "size": Keep.REBUILT,
        "answers_ping": Keep.REBUILT,
        "may_attach": Keep.REBUILT,
        "pipe_connection": Keep.DROPPED,
        # The client offers msgpack again on the connection it opens to
        # the next server.
        "encode": Keep.DROPPED,
        # A peer reconnects to the next server and mirrors again; a
        # tunnel's connections end with the server, and a browser
        # connects again.
        "role": Keep.DROPPED,
        "_tasks": Keep.DROPPED,
        "_before_serving": Keep.DROPPED,
        "closed": Keep.DROPPED,
        "_unanswered": Keep.DROPPED,
        # The pipe's counts start again on the next server's connection.
        "rate_down": Keep.DROPPED,
        "rate_up": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux, pipe_connection: PipeConnection, may_attach: bool = True) -> None:
        self.pymux = pymux

        self.pipe_connection = pipe_connection
        #: How a packet to this client is encoded: JSON until the client
        #: offers msgpack in `start-gui`. Lillecarl/pymux#633.
        self.encode = encode_json

        #: Whether a client on this connection may ask for the user
        #: interface. False for a connection that arrived over the
        #: socket of an integrated server: that server draws on the one
        #: terminal it runs in, and commands are all it serves to
        #: anybody else. Lillecarl/pymux#159.
        self.may_attach = may_attach

        self.size = Size(rows=20, columns=80)
        self.closed = False

        #: What `#{client_rate_down}` and `#{client_rate_up}` read, off
        #: the pipe's counts. Lillecarl/pymux#678.
        self.rate_down = Rate()
        self.rate_up = Rate()
        #: The scope the background work of this connection runs in.
        #: `serve()` opens it, so every task of a client ends when that
        #: client goes, and none of them can outlive the server: the
        #: group is a child of `Pymux.running`'s. Lillecarl/pymux#87.
        self._tasks: anyio.abc.TaskGroup | None = None
        #: What was spawned before `serve()` opened that scope, started
        #: there in order; `None` once it opened. A connection the
        #: server made is served by a `start_soon`, so it can be sent
        #: to before its scope exists: a resumed client lost a packet
        #: at upgrades that way. Lillecarl/pymux#937.
        self._before_serving: list[Coroutine[Any, Any, object]] | None = []

        self.requests = Requests(self)

        #: The client attached on this connection, from `create_app` on
        #: until the connection closes. A socket command makes a
        #: temporary one. Lillecarl/pymux#886.
        self.client: ClientRole | None = None
        #: What this connection is for: its client, the link of a server
        #: that mirrors panes of this one (Lillecarl/pymux#759), or a
        #: tunnel (Lillecarl/pymux#853).
        self.role: Role | None = None

        #: Whether this client answers a ping, and how many pings to
        #: this connection have gone unanswered. Only a far end that
        #: said it answers is ever dropped for not answering.
        #: Lillecarl/pymux#446.
        self.answers_ping = False
        self._unanswered = 0

        self.pymux.serving.serve_connection(self)

    def spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        """
        Run a coroutine in the background of this connection.

        It belongs to the connection's scope, so it ends when the
        client goes and it cannot be collected while it runs. A task
        that nobody holds can be collected mid-flight: the loop then
        reports that, and prompt_toolkit answers an exception in the
        loop by leaving the alternate screen and asking the user to
        press a key. There is no user at that end, so the answer fails
        and reports again -- a lost task becomes an endless storm of
        repaints on the terminal of every client.
        """
        if self._tasks is None and self._before_serving is not None:
            self._before_serving.append(coro)
            return
        if self._tasks is None:
            logger.warning("A task was spawned on a connection that does not serve: %s.", coro.__qualname__)
            coro.close()
            return
        self._tasks.start_soon(self._run_task, coro)

    async def _run_task(self, coro: Coroutine[Any, Any, object]) -> None:
        """
        Run one spawned coroutine, and say what it raised.

        **The failure stays inside the connection.** In a task group an
        exception cancels the siblings and goes up, and up from here is
        every other client of this server. The work a connection spawns
        is the work that answers one client, so the honest blast radius
        is that client: the failure is logged and the connection goes on.
        Lillecarl/pymux#87.
        """
        try:
            await coro
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as error:
            logger.error("A task of this connection failed.", exc_info=error)

    async def serve(self) -> None:
        """
        Read this client until the connection ends.

        This is the scope of the connection: the task group it spawns
        into, and the `AppSession` its application lives in.

        **The session is why this is a coroutine and not a callback.**
        `set_app` saves and restores one attribute of one session
        object, so two clients sharing a session save and restore each
        other's application: the last one to exit puts back a value
        that is two attachments stale, and the application it names is
        then held by a module-level `ContextVar` for as long as the
        server runs, with its layout, its panes and their scrollbacks
        behind it. Lillecarl/pymux#230.

        A session of its own is what prompt_toolkit does for the same
        job -- `contrib/ssh/server.py` wraps each client the same way.
        """
        with create_app_session():
            try:
                async with anyio.create_task_group() as tasks:
                    self._tasks = tasks
                    waiting, self._before_serving = self._before_serving or [], None
                    for coro in waiting:
                        tasks.start_soon(self._run_task, coro)
                    try:
                        await self._read_until_it_ends()
                    finally:
                        # What the client still had running goes with it.
                        self._tasks = None
                        tasks.cancel_scope.cancel()
            finally:
                await self.pipe_connection.linger()

    async def _read_until_it_ends(self) -> None:
        failures = 0

        while True:
            try:
                data = await self.pipe_connection.read()
                self._process(data)
            except BrokenPipeError:
                self.detach_and_close()
                break

            except anyio.get_cancelled_exc_class():
                raise

            except Exception:
                # The read loop must never die silently: log the
                # exception and keep the connection alive.
                logger.exception("Exception while processing client packet.")

                failures += 1
                if failures >= FAILURES_THAT_END_A_CONNECTION:
                    # **A read that keeps failing is a connection that
                    # ended in a way the transport did not name.** With
                    # no count here the loop retries at full speed: one
                    # whole processor and a traceback in the log on
                    # every turn, for as long as the server runs.
                    # Measured at millions of tracebacks in eighteen
                    # minutes. Lillecarl/pymux#329.
                    logger.error(
                        "This client failed %d reads in a row. Closing it.",
                        failures,
                    )
                    self.detach_and_close()
                    break
            else:
                failures = 0

    def _process(self, packet: Message) -> None:
        "Hand a packet to this connection's role, or to the method that handles its kind."
        if self.role is not None and packet.get(Field.CMD) not in _TRANSPORT_PACKETS:
            self.role.handle(packet)
            return
        self.handle_own(packet)

    def handle_own(self, packet: Message) -> None:
        "Hand a packet to the method of this connection for its kind: a client's role has none for it."
        _PACKETS.handle(self, packet)

    @property
    def peer(self) -> PeerLink | None:
        "The peer link this connection is, if it is one."
        return self.role if isinstance(self.role, PeerLink) else None

    @_PACKETS.handles(Packet.WIRE)
    def _on_wire(self, packet: Message) -> None:
        "The client's writer turned to msgpack. The pipe's reader has turned with it already."

    @_PACKETS.handles(Packet.RUN_COMMAND)
    def _on_run_command(self, packet: Message) -> None:
        # Handle this in a task. The command handler can produce output
        # that has to be sent back to the client.
        self.spawn(self.requests.run(packet))

    @_PACKETS.handles(Packet.STREAM_PANE)
    def _on_stream_pane(self, packet: Message) -> None:
        # "Send me this pane as frames." A stream holds the connection
        # and writes many packets, so it is not a command that prints.
        self.spawn(self.requests.stream_pane(packet))

    @_PACKETS.handles(Packet.PEER)
    def _on_peer(self, packet: Message) -> None:
        "Another server mirrors panes of this one over this connection, if the two speak a version in common."
        if self.client is not None:
            return
        upgrade = peer_upgrade(packet.get(Field.VERSION), packet.get(Field.OLDEST))
        if upgrade is not None:
            here = socket.gethostname().partition(".")[0]
            side = "the server that connects" if upgrade == "there" else "the server on %s" % (here,)
            refusal = {
                Field.CMD: Packet.PEER_REFUSED,
                Field.ERROR: "upgrade pymux on %s: a peer link of version %s cannot reach one of %s to %s"
                % (side, packet.get(Field.VERSION, 0), OLDEST_PEER_VERSION, PEER_VERSION),
            }

            # Written before the close, as `detach_and_close` says why.
            async def refuse() -> None:
                await self.write_packet(refusal)
                self.close_connection()

            logger.info("Refusing a peer link of version %s.", packet.get(Field.VERSION, 0))
            self.spawn(refuse())
            return
        host = str(packet.get(Field.SERVER) or "")
        logger.info("A peer%s links, at version %s.", " on %s" % (host,) if host else "", packet.get(Field.VERSION, 0))
        link = PeerLink(self, packet.get(Field.WIRE), peer_version(packet.get(Field.VERSION)), host)
        self.role = link
        self.spawn(link.run())

    @_PACKETS.handles(Packet.TUNNEL)
    def _on_tunnel(self, packet: Message) -> None:
        "A peer asks for a port on the machine a pane runs on."
        if self.client is None:
            tunnel = OwnerTunnel(self)
            self.role = tunnel
            self.spawn(tunnel.run(packet[Field.PANE_ID], packet[Field.DEST_HOST], packet[Field.DEST_PORT]))

    @_PACKETS.handles(Packet.STREAM_IN)
    def _on_stream_in(self, packet: Message) -> None:
        # One input message of a stream, which the stream itself reads:
        # a packet that arrives with no stream running has nowhere to go.
        if self.requests.stream is None:
            logger.warning("Input for a stream that is not running. Ignoring.")
        else:
            self.requests.stream.take(packet.get(Field.DATA, ""))

    @_PACKETS.handles(Packet.SIZE)
    def _on_size(self, packet: Message) -> None:
        rows, columns = packet[Field.DATA]
        self.resized(Size(rows=rows, columns=columns))
        # A SIGWINCH comes for pixels alone too: the terminal moved
        # to a display of another density. Lillecarl/pymux#774. A
        # terminal that reports its size in band says the pixels too.
        client = self.client
        if client is not None and client.graphics.cell_size_known and not client.replies.sizes_in_band:
            client.write_output_raw(CELL_SIZE_QUERY)
            client.flush_output()

    def resized(self, size: Size) -> None:
        client = self.client
        if client is not None and client.recording is not None and size != self.size:
            client.recording.hear("size", "%dx%d" % (size.columns, size.rows))
        self.size = size
        self.pymux.invalidate(Woke.CLIENT_RESIZED)

    @_PACKETS.handles(Packet.START_GUI)
    def _on_start_gui(self, packet: Message) -> None:
        start_gui(self, packet)

    @_PACKETS.handles(Packet.PONG)
    def _on_pong(self, packet: Message) -> None:
        # The client is still there. Any answer clears the count, so a
        # client that is merely slow keeps its place.
        self._unanswered = 0

    def send_packet(self, data: object, compress_after: bool = False) -> None:
        """
        Send packet to client. `compress_after` turns the stream to zstd
        after this one (`PipeConnection.write`).
        """
        if self.closed:
            return
        # Every byte for the client's terminal passes here, the frames
        # and what follows them alike.
        recording = self.client.recording if self.client is not None else None
        if recording is not None and isinstance(data, dict) and data.get(Field.CMD) == Packet.OUT:
            recording.hear("out", data[Field.DATA])
        # Serialised now: the write runs later, and the packet is what
        # was true when it was sent, in the format of the moment.
        self.spawn(self.write_encoded(self.encode(data), compress_after))

    async def say_restarting(self, wait: float, why: str) -> None:
        """
        Tell this client that the next server comes on the same socket,
        how many seconds to wait for it, and why, for the screen it
        draws meanwhile. Awaited, so the packet is written before the
        server stops. Lillecarl/pymux#409.
        """
        await self.write_packet({Field.CMD: Packet.RESTARTING, Field.WAIT: wait, Field.MESSAGE: why})
        if self.closed:
            # `_write` lets a client go on a broken pipe or a full
            # buffer, and that client leaves for good instead of
            # waiting. Lillecarl/pymux#766.
            logger.warning(
                "Client %s went before the restart notice reached it.",
                (self.client.client_id if self.client is not None else "") or "?",
            )

    async def _write_urgent(self, data_obj: object) -> None:
        "Write a packet ahead of every write that waits for the connection: sound."
        if self.closed:
            return
        try:
            await self.pipe_connection.write(self.encode(data_obj), urgent=True)
        except BrokenPipeError:
            self.detach_and_close()

    async def write_packet(self, data_obj: object) -> None:
        """
        Write a packet to the client. (This waits for the write to complete.)
        """
        if self.closed:
            return
        await self.write_encoded(self.encode(data_obj))

    async def write_encoded(self, frame: bytes, compress_after: bool = False) -> None:
        "Write one encoded packet; a client that is gone is let go."
        try:
            await self.pipe_connection.write(frame, compress_after)
        except BrokenPipeError:
            self.detach_and_close()

    def close_connection(self) -> None:
        # Without this the server goes on drawing frames for a client
        # that is no longer connected.
        if self.closed:
            return
        client = self.client
        if client is not None:
            client.close()
        self.pymux.remove_client(self)
        self.client = None
        if self.role is client:
            self.role = None
        self.closed = True
        if client is not None:
            client.went()

        # The background work of this connection ends with the read
        # loop that `serve` runs: leaving that scope cancels the group.
        # This is called from inside those tasks, so cancelling here
        # would cancel the caller before it finished closing.

        with suppress(Exception):
            self.pipe_connection.close()

    @property
    def name(self) -> str:
        """
        What `detach-client -t` selects this client by.

        tmux names a client by its tty and falls back to its process
        when it has none (`server-client.c:2989`), and pymux does the
        same with the machine in front. **The machine is always
        there**, because a pymux client can be on another one: two
        people can each be on their own `/dev/pts/3`, and a tty path
        alone would name them both. Lillecarl/pymux#335.

        A terminal keeps its tty for as long as it is open, so a person
        who detaches and attaches again from the same window types the
        same name. That is the property this is chosen for: a name that
        moves to another client is worse than no name.
        """
        client = self.client
        # Nothing said: a connection that never sent `start-gui` is the
        # temporary client of a command, which nobody selects, or a peer
        # link or a tunnel.
        if client is None:
            return ""
        if client.ttyname:
            what = client.ttyname
        elif client.pid:
            what = "client-%d" % (client.pid,)
        else:
            return ""
        return "%s:%s" % (client.hostname or "?", what)

    async def watch_liveness(self, ping: Callable[[], Awaitable[None]] | None = None) -> None:
        """
        Drop a client or a peer link that stops answering. `ping` sends
        one ping; a client's goes straight out, and a peer link's waits
        in its queue (`PeerLink.ping`).

        **A closed socket is not enough to drop a client**: a socket can
        stay open long after the person is gone: a laptop that sleeps leaves a dead TCP
        connection that the far sshd holds until it gives up, and the
        forwarded unix socket here stays open behind it. Nothing else
        asks, because a client that is attached and quiet sends
        nothing -- it reports its size only when the size changes.

        What the ghost costs is not the socket. `window-size smallest`
        is the default, so a client holding a stale terminal size
        keeps the window that size for whoever is really looking at
        it. Lillecarl/pymux#446.

        **Only a client that said it answers is ever dropped.**
        `start-gui` carries that, the way it carries whether the
        client can forward a port: a client that does not know the
        packet would otherwise be reaped for staying quiet, which is
        the one mistake this must not make.

        A pong clears the count, so a slow link keeps its place and
        only silence ends it.
        """
        while True:
            await anyio.sleep(PING_INTERVAL)

            if self._unanswered >= PING_MISSES:
                logger.info("Dropping %s: %d pings unanswered.", self.name, self._unanswered)
                self.detach_and_close()
                return

            self._unanswered += 1
            if ping is None:
                await self.write_packet({Field.CMD: Packet.PING})
            else:
                await ping()

    def detach_and_close(self, hang_up: bool = False) -> None:
        """
        Take this client off its session and close the connection.

        `hang_up` is `attach -x`: the client leaves and then hangs up
        the process that started it, so the terminal it was in closes.
        The client hears that in the `exit` packet, which is the one
        packet that already means "the server let you go" -- so the
        SSH client reads it as an ending and not as a link that went,
        and does not come back. Lillecarl/pymux#332,
        Lillecarl/pymux#347.

        **The packet has to be written before the close.**
        `send_packet` spawns the write into the scope of this
        connection, and closing cancels that scope, so the two in a row
        would race and the client would usually hear nothing. This
        spawns one task that writes and then closes.
        """
        # A connection with no scope to run the write in cannot be told
        # anything, and a hangup that waited for one would never close
        # it. Leaving is the part that still works.
        if not hang_up or self._tasks is None:
            self.close_connection()
            return

        async def hang_up_and_close() -> None:
            await self.write_packet({Field.CMD: Packet.EXIT, Field.CODE: 0, Field.HANG_UP: True})
            self.close_connection()

        self.spawn(hang_up_and_close())
