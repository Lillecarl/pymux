"""
Who a port forward is asked of, and whether it needs a yes first. The
server holds no SSH connection: a client does, so a forward is a
request to the client, the way `open-url` is. `forwarding.py` holds the
table a client reports back. Lillecarl/pyterm#438, Lillecarl/pyterm#442.

A person who reaches this server through a peer server of theirs
(`connect`) holds the SSH connection in that server, so the request
goes along the peer link, and the peer asks them. Lillecarl/pyterm#976.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from libpymux.protocol import Capability, Message

from ..commands import CommandException
from ..enums import ForwardMode
from .forwarding import LOOPBACK_NAMES

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..mirror.link import PeerLink
    from .forwarding import Forward
    from .main import Pymux

__all__ = ["Asker", "ForwardRequests", "ThroughPeer"]


class Asker(NamedTuple):
    """
    The client a forward is asked of, and whether a person asked.

    `in_person` is false when the request arrived over the socket,
    which is how a program in a pane runs a command. The forward still
    belongs to an attached client -- that is the machine with the SSH
    connection -- but nobody chose it, so `needs_asking` says
    it has to be confirmed. Lillecarl/pyterm#442.
    """

    client_state: ClientState
    in_person: bool


class ThroughPeer(NamedTuple):
    """
    The peer link a forward is asked of: the peer holds the SSH
    connection, and the person attached to it confirms each forward
    there, since nobody here chose it.
    """

    link: PeerLink


class ForwardRequests:
    "Sends the forwards a person asks for to the client that can make them."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    def client(self) -> Asker | ThroughPeer:
        """
        The client that a forward is asked of, and who asked.

        **The one that asked, when a client asked.** A forward binds a
        port on one machine, and the machine a person means is the one
        they typed the command on. That is different from `open-url`,
        which a program in a pane triggers and which therefore has to
        choose a client; here there is nothing to choose while the
        person is right there.

        A command that arrived over the socket has a temporary client
        of its own, which is no terminal and holds no SSH connection.
        Then the one attached client is the answer, and two of them is
        a question this cannot answer for the person.
        Lillecarl/pyterm#438.

        **Only clients that can forward count.** A person on the
        server's own machine, or on a phone that runs `pymux attach`
        there, is attached and forwards nothing, so beside one SSH
        client they leave nothing to choose. Lillecarl/pyterm#975.

        **A peer link counts when no client can forward.** Then the one
        peer whose link crossed SSH is the answer, and two is a question
        for the person again. Lillecarl/pyterm#976.
        """
        if self.pymux.a_person_asked():
            return self._can_it_forward(Asker(self.pymux.get_client_state(), in_person=True))

        attached = self.pymux.clients
        forwarding = [one for one in attached if one.client.can_forward]
        if not forwarding:
            links = [link for link in self.pymux.peer_links() if link.can(Capability.FORWARDS)]
            if len(links) > 1:
                raise CommandException(
                    "Several peer servers link over SSH. Run this from a client attached to the one that should forward."
                )
            if links:
                return ThroughPeer(links[0])
        if not attached:
            raise CommandException("Nobody is attached, so there is nothing to forward through.")
        attached = forwarding or attached
        if len(attached) > 1:
            raise CommandException("Several clients are attached. Run this from the one that should forward.")
        # A temporary client is what a command that arrived
        # over the socket, which is how a program in a pane runs one.
        # Nobody typed this.
        return self._can_it_forward(Asker(attached[0], in_person=False))

    def _can_it_forward(self, asker: Asker) -> Asker:
        """
        Refuse a client with no SSH connection, before anything else.

        **Before the question**, because asking somebody whether to
        make a forward that cannot be made wastes the only answer they
        can give.
        """
        if not asker.client_state.client.can_forward:
            raise CommandException(
                "%s did not reach this server over SSH, so it cannot forward a port."
                % (asker.client_state.client.connection.name,)
            )
        return asker

    def needs_asking(self, forward: Forward, in_person: bool) -> bool:
        """
        Whether this forward has to be confirmed before it is made.

        **"on" is not "never ask".** Two cases ask whatever the option
        says, because in neither of them did the person at the keyboard
        choose this forward:

        - A program in a pane asked. Any command a pane runs reaches
          the server, so without this any program on the far machine
          could bind a port here.
        - It listens somewhere other than loopback, which publishes the
          far machine's service to the network this one is on. openssh
          makes the same split, through `GatewayPorts`.

        Lillecarl/pyterm#442.
        """
        if self.pymux.options.forward_mode == ForwardMode.ASK:
            return True

        return not in_person or forward.listen_host not in LOOPBACK_NAMES

    def send(self, asker: Asker | ThroughPeer, packet: Message) -> None:
        """
        Ask a client or a peer to open or close a forward.

        **The server never forwards anything itself.** It holds no SSH
        connection: the client does, and it is the machine the person
        sits at. So this is a request, the way `open-url` is, and the
        answer comes back as the client's whole table.

        `client` has already refused a client that cannot
        forward at all. Lillecarl/pyterm#438.
        """
        if isinstance(asker, ThroughPeer):
            asker.link.queue(packet)
            return
        asker.client_state.client.connection.send_packet(packet)
