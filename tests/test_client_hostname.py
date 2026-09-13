"""
A client says which machine it runs on, and the server keeps it.

**The server cannot find this out.** `socket.gethostname()` in the
server answers the server's own machine. Over ssh (`client/ssh.py`)
that is another machine than the one the person sits at, and that is
the case where the answer is worth having: a view that lists the
clients of a session has nothing to name them by otherwise.

So the name travels in the `start-gui` packet, beside `term` and
`colorterm`, and `ServerConnection` keeps it the way it keeps the
colours the terminal reported. Lillecarl/pymux#287.
"""

import socket

from prompt_toolkit.data_structures import Size

from pymux.client.terminal import TerminalClient
from session import OTHER_MACHINE, in_this_process, over_connection

SIZE = Size(rows=24, columns=80)


class _Listening(TerminalClient):
    "A client whose packets go to a list instead of a server."

    def __init__(self) -> None:
        super().__init__()
        self.packets: list = []

    def size(self):
        # The real one asks the terminal, and a test runner has none.
        return 24, 80

    def _send_packet(self, data) -> None:
        self.packets.append(data)


def test_a_client_says_which_machine_it_runs_on(capfd):
    client = _Listening()

    client._start_gui(detach_other_clients=False, color_depth=None)

    started = [p for p in client.packets if p["cmd"] == "start-gui"]
    assert started, client.packets
    assert started[0]["hostname"] == socket.gethostname()


async def test_the_server_keeps_the_hostname_of_its_client():
    async with over_connection() as session:
        state, _size = await session.attach("the client", SIZE)

        assert state.connection.hostname == OTHER_MACHINE


async def test_each_client_keeps_its_own():
    "The name belongs to the connection, not to the server."
    async with over_connection() as session:
        here, _ = await session.attach("here", SIZE, hostname="workstation")
        there, _ = await session.attach("there", SIZE, hostname="buildbox-7")

        assert here.connection.hostname == "workstation"
        assert there.connection.hostname == "buildbox-7"


async def test_the_other_route_says_it_too():
    "A driver cannot tell the routes apart, so both take a hostname."
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE, hostname="workstation")

        assert state.connection.hostname == "workstation"
