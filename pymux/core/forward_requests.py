"""
Who a port forward is asked of, and whether it needs a yes first. The
server holds no SSH connection: a client does, so a forward is a
request to the client, the way `open-url` is. `forwarding.py` holds the
table a client reports back. Lillecarl/pymux#436, Lillecarl/pymux#440.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from libpymux.protocol import Message

from ..commands import CommandException
from ..enums import ForwardMode
from .forwarding import LOOPBACK_NAMES

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from .forwarding import Forward
    from .main import Pymux

__all__ = ["Asker", "ForwardRequests"]


class Asker(NamedTuple):
    """
    The client a forward is asked of, and whether a person asked.

    `in_person` is false when the request arrived over the socket,
    which is how a program in a pane runs a command. The forward still
    belongs to an attached client -- that is the machine with the SSH
    connection -- but nobody chose it, so `needs_asking` says
    it has to be confirmed. Lillecarl/pymux#440.
    """

    client_state: ClientState
    in_person: bool


class ForwardRequests:
    "Sends the forwards a person asks for to the client that can make them."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    def client(self) -> Asker:
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
        Lillecarl/pymux#436.
        """
        if self.pymux.a_person_asked():
            return self._can_it_forward(Asker(self.pymux.get_client_state(), in_person=True))

        attached = self.pymux.clients
        if not attached:
            raise CommandException("Nobody is attached, so there is nothing to forward through.")
        if len(attached) > 1:
            raise CommandException("Several clients are attached. Run this from the one that should forward.")
        # A temporary client is the fake CLI of a command that arrived
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
        if not asker.client_state.connection.can_forward:
            raise CommandException(
                "%s did not reach this server over SSH, so it cannot forward a port."
                % (asker.client_state.connection.name,)
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

        Lillecarl/pymux#440.
        """
        if self.pymux.options.forward_mode == ForwardMode.ASK:
            return True

        return not in_person or forward.listen_host not in LOOPBACK_NAMES

    def send(self, client_state: ClientState, packet: Message) -> None:
        """
        Ask a client to open or close a forward.

        **The server never forwards anything itself.** It holds no SSH
        connection: the client does, and it is the machine the person
        sits at. So this is a request, the way `open-url` is, and the
        answer comes back as the client's whole table.

        `client` has already refused a client that cannot
        forward at all. Lillecarl/pymux#436.
        """
        client_state.connection.send_packet(packet)
