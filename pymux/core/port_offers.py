"""
Offer to forward a port when a program in a pane starts listening on
one, the way VS Code's remote editor does. Only while `forward-offers`
is on. Lillecarl/pymux#438.

**A poll, because nothing says.** No call tells a process that another
one bound a port, short of netlink. So every `OFFER_POLL` seconds this
reads the listening sockets of each pane's process tree, through
psutil, which reads them on Linux and on macOS alike. It reads only
while the option is on and a client that can forward is attached.

**Once a pane, once a port.** A port that was offered is not offered
again until its pane ends, whatever the answer was. Otherwise a dev
server that restarts on every save would ask on every save.

The offer is a question, and a yes runs `forward-port -c -L`, so the
forward is made by the command a person could have typed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import anyio
from pyte.keep import Keep

from ..enums import ForwardMode, Woke
from ..log import logger
from .forwarding import Direction

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..ids import PaneId
    from .main import Pymux

__all__ = ["OFFER_POLL", "PortOffers", "listening_ports"]

#: How often the panes are read for new listeners, in seconds. Two is
#: about how long a person waits after a dev server says it is up.
OFFER_POLL = 2.0

#: The addresses a forward to "localhost" on the server's machine
#: reaches. A program that listens on one other address -- a container's
#: bridge, a LAN address -- is not reached that way, so it is not offered.
_REACHED = frozenset({"127.0.0.1", "::1", "0.0.0.0", "::"})


def listening_ports(pid: int) -> set[int]:
    """
    The TCP ports that `pid` and its children listen on, where a
    forward to localhost reaches them. **It blocks**: psutil reads the
    process table.
    """
    import psutil

    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return set()

    ports: set[int] = set()
    for process in processes:
        try:
            connections = process.net_connections(kind="tcp")
        except psutil.Error:
            # It ended, or it is somebody else's.
            continue
        for connection in connections:
            if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.ip in _REACHED:
                ports.add(connection.laddr.port)
    return ports


class PortOffers:
    "The ports this server offered to forward, by the pane that listens on each."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # An upgrade offers again what it forgets. The offers are a
        # convenience, and a stale one would hide a new listener.
        "offered": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.offered: dict[PaneId, set[int]] = {}

    async def keep(self) -> None:
        "Look for new listeners, for as long as the server runs."
        while True:
            await anyio.sleep(OFFER_POLL)
            try:
                await self.look()
            except Exception:
                logger.exception("Looking for ports to offer failed.")

    async def look(self) -> None:
        "Read every pane's listeners once, off the loop, and offer the new ones."
        options = self.pymux.options
        if not options.forward_offers or options.forward_mode == ForwardMode.OFF:
            return
        if not any(client.connection.can_forward for client in self.pymux.clients):
            return

        pids = {
            pane_id: pane.process.backend.pid
            for pane_id, pane in list(self.pymux.panes_by_id.items())
            if pane.process.backend.pid
        }
        found = await anyio.to_thread.run_sync(lambda: {pane_id: listening_ports(pid) for pane_id, pid in pids.items()})
        self.offer(found)

    def offer(self, found: dict[PaneId, set[int]]) -> None:
        "Ask about each port in `found` that its pane has not been asked about."
        for gone in set(self.offered) - set(found):
            del self.offered[gone]

        asked = False
        for pane_id, ports in found.items():
            offered = self.offered.setdefault(pane_id, set())
            for port in sorted(ports - offered):
                offered.add(port)
                client = self._client_for(pane_id)
                if client is None or _forwards(client, port):
                    continue
                client.ask(
                    "%s listens on port %d. Forward it to this machine? (y/n)" % (pane_id, port),
                    "forward-port -c -L %d:localhost:%d" % (port, port),
                )
                asked = True
        if asked:
            self.pymux.invalidate(Woke.PORT_OFFERED)

    def _client_for(self, pane_id: PaneId) -> ClientState | None:
        "Who is asked: the client of the pane's session that can forward and was used last."
        pane = self.pymux.panes_by_id.get(pane_id)
        session = self.pymux.session_holding(pane) if pane is not None else None
        if session is None:
            return None
        able = [client for client in self.pymux.clients_on(session) if client.connection.can_forward]
        if not able:
            return None
        return max(able, key=lambda client: client.last_used)


def _forwards(client: ClientState, port: int) -> bool:
    "Whether this client forwards a port here to `port` on the server's machine already."
    return any(
        row.get("direction") == str(Direction.LOCAL) and str(row.get("dest", "")).rpartition(":")[2] == str(port)
        for row in client.connection.forwards
    )
