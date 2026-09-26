"""
The forwarded ports of one SSH client, and the listeners behind them.

**The client owns this table and the server holds a copy to draw.** A
forward is a call on the `asyncssh` connection that `client/ssh.py`
already has, so only this side can make one, and the far side is a
normal sshd that gains nothing. It is the property the whole `ssh://`
client is built on. Lillecarl/pymux#436.

**A reconnect is what this class is for.** `SshClient._link_again`
builds a new connection, and every listener of the old one dies with
it. So the table keeps what the person asked for, separately from what
is open, and opens the wanted set again on the new connection. openssh
cannot do this: `~C` and its forwards go with the link.

`pymux/forwarding.py` holds what a forward is. This holds the live ones.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable, NamedTuple

from pymux.forwarding import ANY_PORT, Direction, Forward

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection, SSHListener

__all__ = [
    "Forwards",
    "Opened",
    "Wanted",
]

#: What names a forward for removing it, and what two forwards may not
#: share: the machine that listens, and where on it.
Where = tuple[Direction, str, int]


class Wanted(NamedTuple):
    """
    One forward as it was asked for.

    **`idle` is what separates the two kinds.** A person's forward has
    none: it listens exactly where they said, and it stays until they
    remove it. A forward pymux opened by itself, for a loopback URL,
    has one -- and with it comes the licence to move, because the URL
    is rewritten to whatever port was really free, so the number was
    never the person's to begin with. Lillecarl/pymux#437.
    """

    forward: Forward
    idle: float | None = None

    @property
    def may_move(self) -> bool:
        "Whether another port will do when the one asked for is taken."
        return self.idle is not None


class Opened(NamedTuple):
    """
    One forward as it stands, for the server to draw.

    `port` is the number really bound. It differs from the number
    asked for when the person asked for any, and when a URL's own port
    was taken here. `error` is why it is not open, and is empty while
    it is.
    """

    forward: Forward
    port: int
    error: str

    def report(self) -> dict:
        """
        What crosses the wire to the server's copy.

        The port is the one really bound and not the one asked for, so
        that the listing shows a person the number they can connect to.
        """
        return {
            "direction": str(self.forward.direction),
            "listen_host": self.forward.listen_host,
            "port": self.port,
            "dest": self.forward.dest,
            "error": self.error,
        }


class Forwards:
    """
    What this client was asked to forward, and what is open of it.

    **Wanting and having are kept apart on purpose.** A forward that
    the far sshd refuses, or whose port is busy here, stays in the
    table with the reason beside it: the person sees why, and the next
    reconnect tries it again rather than forgetting it.
    """

    def __init__(self) -> None:
        self._wanted: dict[Where, Wanted] = {}
        self._open: dict[Where, SSHListener] = {}
        self._ports: dict[Where, int] = {}
        self._errors: dict[Where, str] = {}
        #: When each forward last carried a connection. Only a forward
        #: with an idle time has one, and only that one is reaped.
        self._used: dict[Where, float] = {}

    def __len__(self) -> int:
        return len(self._wanted)

    def _now(self) -> float:
        """
        The clock the idle time is measured on.

        **The world's clock and not the loop's.** A laptop that slept
        for eight hours really has left a forward unused for eight
        hours, and `time.monotonic` does not run while it sleeps. A
        method so that a test can move it without waiting.
        """
        return time.time()

    # ------------------------------------------------------------------
    # What the person asked for.

    async def add(
        self,
        connection: SSHClientConnection,
        forward: Forward,
        idle: float | None = None,
    ) -> Opened:
        """
        Want this forward, and open it now.

        A forward that listens where one already does replaces it, so
        that asking twice is not two listeners racing for one port.

        **A forward pymux made for itself never replaces one.** `idle`
        is what says it is one: pymux chose the port from a URL, and a
        person who typed `forward-port` for that same port chose it on
        purpose. Taking theirs away would be pymux answering a question
        nobody asked it. Lillecarl/pymux#437.
        """
        where = _where(forward)
        wanted = Wanted(forward, idle)
        standing = self._wanted.get(where)

        if wanted.may_move and standing is not None:
            if standing.forward != forward:
                return Opened(
                    forward,
                    forward.listen_port,
                    "%s already goes to %s" % (forward.listen, standing.forward.dest),
                )
            if where in self._open:
                # The same URL again. The listener is up, so this is a
                # use of it and not a new one.
                self._wanted[where] = wanted
                self._used[where] = self._now()
                return self._one(where)

        self.remove(where)
        self._wanted[where] = wanted
        return await self._open_one(connection, wanted)

    def remove(self, where: Where) -> Forward | None:
        "Stop wanting the forward that listens here, and close it."
        self._close_one(where)
        self._ports.pop(where, None)
        self._errors.pop(where, None)
        self._used.pop(where, None)
        wanted = self._wanted.pop(where, None)
        return None if wanted is None else wanted.forward

    def reap(self, now: float | None = None) -> list[Forward]:
        """
        Close and forget every forward nothing has used lately.

        Only a forward with an idle time, which is one pymux opened for
        a URL. A person's forward is never reaped: they asked for it.

        **Idle means no new connection**, and nothing more. asyncssh
        tells this table when a connection arrives and never when one
        ends, so a single long-lived connection -- a websocket that a
        page holds open -- reads as idle while it carries bytes.
        Closing the listener does not cut it: `SSHListener.close` stops
        new connections and leaves open ones alone. What breaks is the
        page's *next* request, and opening the URL again brings the
        forward back. That is why the default idle time is generous.
        Lillecarl/pymux#437.
        """
        if now is None:
            now = self._now()

        gone = []
        for where, wanted in list(self._wanted.items()):
            if wanted.idle is None:
                continue
            if now - self._used.get(where, now) < wanted.idle:
                continue
            gone.append(wanted.forward)
            self.remove(where)

        return gone

    def close(self) -> None:
        "Close every listener, and keep wanting them."
        for where in list(self._open):
            self._close_one(where)

    # ------------------------------------------------------------------
    # The connection under them.

    async def reopen(self, connection: SSHClientConnection) -> str:
        """
        Open the wanted set on a new connection.

        **Nothing here raises.** This runs on the way back from a link
        that dropped, and a port that has since been taken must not
        stop the person's panes coming back. The reason lands beside
        the forward instead, where `list-forwards` shows it.

        Answers what to tell the person, or "" when every forward came
        back exactly as it was. **A forward that came back on another
        port is the case worth a sentence.** One that asked for any
        free port rarely gets the same one twice, and whatever was
        pointed at the old number -- a browser tab, a CDP client, a
        script -- is pointing at nothing. Saying the new number is the
        only way to find out short of `list-forwards`.
        Lillecarl/pymux#442.
        """
        # A forward whose idle time ran out while the link was down has
        # nothing to come back for.
        self.reap()

        was = dict(self._ports)
        self._open.clear()

        moved = []
        lost = []

        for wanted in list(self._wanted.values()):
            forward = wanted.forward
            opened = await self._open_one(connection, wanted)
            where = _where(forward)

            if opened.error:
                lost.append("%s (%s)" % (forward.spell(), opened.error))
            elif where in was and was[where] != opened.port:
                moved.append(
                    "%s is now on %d" % (forward.spell(), opened.port)
                )

        return " ".join(
            part
            for part in (
                "Forwarding moved: %s." % ("; ".join(moved),) if moved else "",
                "Could not forward %s." % ("; ".join(lost),) if lost else "",
            )
            if part
        )

    async def _open_one(
        self, connection: SSHClientConnection, wanted: Wanted
    ) -> Opened:
        forward = wanted.forward
        where = _where(forward)

        #: **The name of a forward stays the port it was asked for.**
        #: A second try on any free port is a different bind and the
        #: same forward: `Where` holds the URL's number, `_ports` holds
        #: the one really bound, and the URL is rewritten to that. Two
        #: URL forwards that both fall back therefore keep their own
        #: entries -- with `ANY_PORT` in the name they would share one
        #: and the second would close the first. Lillecarl/pymux#441.
        tries = [forward]
        if wanted.may_move and forward.listen_port != ANY_PORT:
            tries.append(forward._replace(listen_port=ANY_PORT))

        accepted = self._accept(where) if wanted.may_move else None
        error = ""

        for attempt in tries:
            try:
                listener = await _listen(connection, attempt, accepted)
            except Exception as raised:
                error = _why(raised)
                continue

            self._open[where] = listener
            self._errors.pop(where, None)
            # The number really bound, which is news when the person
            # asked for any free port. Both directions answer it: a
            # local listener reports what the operating system gave,
            # and a remote one reports what the far sshd put in its
            # `tcpip-forward` reply. The fallback is for a listener
            # that reports nothing.
            self._ports[where] = listener.get_port() or attempt.listen_port
            self._used[where] = self._now()
            return self._one(where)

        self._errors[where] = error
        self._ports[where] = forward.listen_port
        return Opened(forward, forward.listen_port, error)

    def _accept(self, where: Where) -> Callable[[str, int], bool]:
        """
        What marks a forward as used, for the idle time to measure.

        asyncssh calls this per incoming connection and takes the
        answer as permission, so it has to say yes. Only a local
        forward has one: `forward_remote_port` takes no accept handler,
        and a URL forward is always local.
        """

        def accepted(_host: str, _port: int) -> bool:
            self._used[where] = self._now()
            return True

        return accepted

    def _close_one(self, where: Where) -> None:
        listener = self._open.pop(where, None)
        if listener is not None:
            listener.close()

    # ------------------------------------------------------------------

    def _one(self, where: Where) -> Opened:
        wanted = self._wanted[where]
        return Opened(
            wanted.forward,
            self._ports.get(where, wanted.forward.listen_port),
            self._errors.get(where, ""),
        )

    def opened(self) -> list[Opened]:
        "Every wanted forward, with the port it got or the reason it has none."
        return [self._one(where) for where in self._wanted]

    def report(self) -> list[dict]:
        "The whole table, for the copy the server draws."
        return [one.report() for one in self.opened()]


def _where(forward: Forward) -> Where:
    return (forward.direction, forward.listen_host, forward.listen_port)


async def _listen(
    connection: SSHClientConnection,
    forward: Forward,
    accepted: Callable[[str, int], bool] | None = None,
) -> SSHListener:
    """
    Ask asyncssh for the listener of this forward.

    The two calls sit where openssh's two flags sit: `forward_local_port`
    listens on this machine and the far sshd opens the destination,
    `forward_remote_port` asks the far sshd to listen and opens the
    destination from here.
    """
    if forward.direction is Direction.LOCAL:
        return await connection.forward_local_port(
            forward.listen_host,
            forward.listen_port,
            forward.dest_host,
            forward.dest_port,
            accepted,
        )

    return await connection.forward_remote_port(
        forward.listen_host,
        forward.listen_port,
        forward.dest_host,
        forward.dest_port,
    )


def _why(error: Exception) -> str:
    """
    What to show a person about a forward that did not open.

    asyncssh raises `ChannelListenError` when the far sshd refuses,
    which is nearly always `AllowTcpForwarding no` or a port already
    taken there, and `OSError` when a local bind fails. Neither spells
    itself usefully with the class name in front, so this takes the
    message and falls back to the class only when there is none.
    """
    message = str(error).strip()
    return message or type(error).__name__
