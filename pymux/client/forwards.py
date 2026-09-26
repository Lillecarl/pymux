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

from typing import TYPE_CHECKING, NamedTuple

from pymux.forwarding import Direction, Forward

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection, SSHListener

__all__ = [
    "Forwards",
    "Opened",
]

#: What names a forward for removing it, and what two forwards may not
#: share: the machine that listens, and where on it.
Where = tuple[Direction, str, int]


class Opened(NamedTuple):
    """
    One forward as it stands, for the server to draw.

    `port` is the number really bound, which differs from the number
    asked for when the person asked for any. `error` is why it is not
    open, and is empty while it is.
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
        self._wanted: dict[Where, Forward] = {}
        self._open: dict[Where, SSHListener] = {}
        self._ports: dict[Where, int] = {}
        self._errors: dict[Where, str] = {}

    def __len__(self) -> int:
        return len(self._wanted)

    # ------------------------------------------------------------------
    # What the person asked for.

    async def add(self, connection: SSHClientConnection, forward: Forward) -> Opened:
        """
        Want this forward, and open it now.

        A forward that listens where one already does replaces it, so
        that asking twice is not two listeners racing for one port.
        """
        where = _where(forward)
        self.remove(where)
        self._wanted[where] = forward
        return await self._open_one(connection, forward)

    def remove(self, where: Where) -> Forward | None:
        "Stop wanting the forward that listens here, and close it."
        self._close_one(where)
        self._ports.pop(where, None)
        self._errors.pop(where, None)
        return self._wanted.pop(where, None)

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
        was = dict(self._ports)
        self._open.clear()

        moved = []
        lost = []

        for forward in list(self._wanted.values()):
            opened = await self._open_one(connection, forward)
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
        self, connection: SSHClientConnection, forward: Forward
    ) -> Opened:
        where = _where(forward)

        try:
            listener = await _listen(connection, forward)
        except Exception as error:
            self._errors[where] = _why(error)
            self._ports[where] = forward.listen_port
            return Opened(forward, forward.listen_port, _why(error))

        self._open[where] = listener
        self._errors.pop(where, None)
        # The number really bound, which is news when the person asked
        # for any free port. Both directions answer it: a local
        # listener reports what the operating system gave, and a remote
        # one reports what the far sshd put in its `tcpip-forward`
        # reply. The fallback is for a listener that reports nothing.
        port = listener.get_port() or forward.listen_port
        self._ports[where] = port
        return Opened(forward, port, "")

    def _close_one(self, where: Where) -> None:
        listener = self._open.pop(where, None)
        if listener is not None:
            listener.close()

    # ------------------------------------------------------------------

    def opened(self) -> list[Opened]:
        "Every wanted forward, with the port it got or the reason it has none."
        return [
            Opened(
                forward,
                self._ports.get(where, forward.listen_port),
                self._errors.get(where, ""),
            )
            for where, forward in self._wanted.items()
        ]

    def report(self) -> list[dict]:
        "The whole table, for the copy the server draws."
        return [one.report() for one in self.opened()]


def _where(forward: Forward) -> Where:
    return (forward.direction, forward.listen_host, forward.listen_port)


async def _listen(
    connection: SSHClientConnection, forward: Forward
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
