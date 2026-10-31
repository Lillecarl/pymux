"""
A port a program in a pane starts listening on is offered to the client
that can forward it, once, while `forward-offers` is on.
Lillecarl/pymux#438.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import anyio

from pymux.core.forwarding import Direction
from pymux.core.port_offers import PortOffers, listening_ports
from pymux.enums import ForwardMode

#: A child that listens on a free loopback port and says which.
LISTENS = (
    "import socket, sys, time\n"
    "s = socket.socket()\n"
    "s.bind(('127.0.0.1', 0))\n"
    "s.listen()\n"
    "print(s.getsockname()[1], flush=True)\n"
    "time.sleep(30)\n"
)


async def test_a_listener_in_the_process_tree_is_found():
    async with await anyio.open_process([sys.executable, "-c", LISTENS]) as child:
        try:
            assert child.stdout is not None
            with anyio.fail_after(10):
                port = int((await child.stdout.receive()).decode().split()[0])
            found = await anyio.to_thread.run_sync(listening_ports, os.getpid())
            assert port in found
        finally:
            child.kill()


def test_a_process_that_is_gone_listens_on_nothing():
    # 2**22 is above the largest pid Linux hands out by default.
    assert listening_ports(2**22 + 1) == set()


class Client:
    "A client that can forward, and keeps the questions it is asked."

    def __init__(self, forwards=()) -> None:
        self.asked: list[tuple[str, str]] = []
        self.connection = SimpleNamespace(can_forward=True, forwards=list(forwards))
        self.last_used = 0

    def ask(self, text: str, command: str) -> None:
        self.asked.append((text, command))


def offers_to(client: Client) -> PortOffers:
    "Offers whose every pane belongs to `client`."
    offers = PortOffers(SimpleNamespace(invalidate=lambda reason: None))
    offers._client_for = lambda pane_id: client
    return offers


def test_a_new_port_is_offered_with_the_command_a_yes_runs():
    client = Client()
    offers_to(client).offer({"%3": {3000}})
    assert client.asked == [
        ("%3 listens on port 3000. Forward it to this machine? (y/n)", "forward-port -c -L 3000:localhost:3000")
    ]


def test_a_port_is_offered_once_a_pane():
    "Whatever the answer: a dev server that restarts on every save would ask on every save."
    client = Client()
    offers = offers_to(client)
    offers.offer({"%3": {3000}})
    offers.offer({"%3": {3000}})
    offers.offer({"%3": {3000, 5173}})
    assert [command for _text, command in client.asked] == [
        "forward-port -c -L 3000:localhost:3000",
        "forward-port -c -L 5173:localhost:5173",
    ]


def test_a_pane_that_ended_takes_its_offers_with_it():
    client = Client()
    offers = offers_to(client)
    offers.offer({"%3": {3000}})
    offers.offer({})
    assert offers.offered == {}


def test_a_port_already_forwarded_is_not_offered():
    client = Client([{"direction": str(Direction.LOCAL), "dest": "localhost:3000"}])
    offers_to(client).offer({"%3": {3000}})
    assert client.asked == []


async def test_nothing_is_read_while_the_option_is_off(monkeypatch):
    read: list[int] = []
    monkeypatch.setattr("pymux.core.port_offers.listening_ports", lambda pid: read.append(pid) or set())
    pane = SimpleNamespace(process=SimpleNamespace(backend=SimpleNamespace(pid=1)))
    pymux = SimpleNamespace(
        options=SimpleNamespace(forward_offers=False, forward_mode=ForwardMode.ON),
        clients=[Client()],
        panes_by_id={"%3": pane},
    )
    await PortOffers(pymux).look()
    assert read == []

    pymux.options.forward_offers = True
    offers = PortOffers(pymux)
    offers.offer = lambda found: None
    await offers.look()
    assert read == [1]
