"""
What a server asks of an SSH client's link: a port to forward, a URL
whose port has to come here first, and a browser whose port has to go
the other way. Lillecarl/pymux#436, Lillecarl/pymux#437,
Lillecarl/pymux#653.

**The server asks and this side acts**, the way it does for a URL to
open: only the machine a person sits at can bind their port, and only
this client holds the SSH connection that carries it. The table of
forwards is the client's, `SshClient.forwards`, because it outlives a
connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
from libpymux.protocol import Field, Message, Packet

from pymux.core.forwarding import MAY_NARROW, Direction, Forward, the_far_side_may_narrow, with_port

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from .ssh import SshClient

__all__ = [
    "REAP_POLL",
    "browser_asked",
    "forward_asked",
    "open_asked",
    "reap_forwards",
    "report_forwards",
]

#: How often to look for a forward nothing is using, in seconds.
#:
#: It decides only how late a reap is, never whether one happens: the
#: idle time itself is measured against the clock. Half a minute is
#: nothing beside the ten minutes a URL forward is given.
#: Lillecarl/pymux#437.
REAP_POLL = 30.0


async def forward_asked(client: SshClient, connection: SSHClientConnection, packet: Message) -> None:
    """
    Add or remove one forward, because the server asked.

    The answer is the whole table and not the one that changed. It is a
    handful of lines, it makes the server's copy right after a
    reconnect as well as after a change, and a person reading
    `list-forwards` wants the set anyway. Lillecarl/pymux#261.
    """
    if packet.get(Field.REMOVE):
        where = (
            Direction(packet[Field.DIRECTION]),
            packet[Field.LISTEN_HOST],
            packet[Field.LISTEN_PORT],
        )
        gone = client.forwards.remove(where)

        if gone is None:
            said = "Nothing was forwarding %s:%s." % (
                packet[Field.LISTEN_HOST],
                packet[Field.LISTEN_PORT],
            )
        else:
            said = "Stopped forwarding %s." % (gone.spell(),)

        report_forwards(client, said)
        return

    forward = Forward(
        direction=Direction(packet[Field.DIRECTION]),
        listen_host=packet[Field.LISTEN_HOST],
        listen_port=packet[Field.LISTEN_PORT],
        dest_host=packet[Field.DEST_HOST],
        dest_port=packet[Field.DEST_PORT],
    )
    opened = await client.forwards.add(connection, forward)

    if opened.error:
        said = "Cannot forward %s: %s" % (forward.spell(), opened.error)
    elif opened.port != forward.listen_port:
        # The person asked for any free port, so the number is news.
        said = "Forwarding %s:%d to %s." % (
            forward.listen_host,
            opened.port,
            forward.dest,
        )
    else:
        said = "Forwarding %s." % (forward.spell(),)

    if not opened.error and the_far_side_may_narrow(forward):
        # **The listener is open and the address may not be the one
        # asked for.** Saying "Forwarding -R 0.0.0.0:2222" flat
        # would be the client asserting something it cannot know:
        # the reply to `tcpip-forward` carries a port and no
        # address. Lillecarl/pymux#444.
        said = "%s Asked for %s, but %s." % (
            said,
            forward.listen_host,
            MAY_NARROW,
        )

    report_forwards(client, said)


async def open_asked(client: SshClient, connection: SSHClientConnection, packet: Message) -> None:
    """
    Forward the port a URL names, then open the URL.

    **The port first, and the page after it.** A browser that arrives
    before the listener does gets a refused connection and an error
    page, and reloading is then the person's job.

    The URL moves when its own port is taken here. Nothing else can say
    so: the server named the address that a pane printed, and by the
    time this knows the real number the packet has already been sent.
    So the message about it comes from this side. Lillecarl/pymux#437.
    """
    url = packet[Field.DATA]
    asked = packet[Field.FORWARD]
    host, port = asked["host"], asked["port"]

    opened = await client.forwards.add(
        connection,
        Forward(Direction.LOCAL, host, port, host, port),
        idle=asked["idle"],
    )

    if opened.error:
        said = "Opened %s, but not %s:%d: %s" % (
            url,
            host,
            port,
            opened.error,
        )
    else:
        if opened.port != port:
            url = with_port(url, opened.port)
        said = "Opened %s in the browser of this machine." % (url,)

    reached = client.browsers.open_url(url)
    report_forwards(client, said if reached else "")

    if not reached:
        client.send_packet({Field.CMD: Packet.OPEN_FAILED, Field.DATA: url})


async def browser_asked(client: SshClient, connection: SSHClientConnection, packet: Message) -> None:
    """
    Start the browser a program on the server asked for, forward its
    port from the server's machine to this one, and say where it
    listens, or why it does not. The forward goes with the browser.
    Lillecarl/pymux#653.
    """
    port = packet[Field.PORT]

    async def forward(port: int) -> str | None:
        opened = await client.forwards.add(connection, Forward(Direction.REMOTE, "127.0.0.1", port, "127.0.0.1", port))
        if opened.error:
            return "cannot forward port %d from the server: %s" % (port, opened.error)
        return None

    try:
        await client.browsers.run(packet, forward)
    finally:
        client.forwards.remove((Direction.REMOTE, "127.0.0.1", port))


async def reap_forwards(client: SshClient) -> None:
    "Give back the port of a URL that nobody is using any more."
    while True:
        await anyio.sleep(REAP_POLL)

        gone = client.forwards.reap()
        if gone:
            report_forwards(
                client, "Stopped forwarding %s: nothing used it." % ("; ".join(one.listen for one in gone),)
            )


def report_forwards(client: SshClient, message: str = "") -> None:
    """
    Tell the server what this client is forwarding.

    The server keeps a copy to draw and never the truth: it has no SSH
    connection, so it cannot know whether a listener is really open.
    `list-forwards` reads the copy.
    """
    client.send_packet(
        {
            Field.CMD: Packet.FORWARDS,
            Field.DATA: client.forwards.report(),
            Field.MESSAGE: message,
        }
    )
