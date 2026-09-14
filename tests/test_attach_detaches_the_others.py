"""
`pymux attach -d` takes the session away from the other clients.

The flag rides on the `start-gui` packet, which is where the server
reads it. Lillecarl/pymux#344.
"""

import json

import anyio

from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection


def _server() -> Pymux:
    pymux = Pymux()
    pymux.test_mode = True
    return pymux


def _start_gui(detach_others: bool) -> str:
    return json.dumps(
        {
            "cmd": "start-gui",
            "detach-others": detach_others,
            "color-depth": None,
            "term": "xterm-256color",
            "colorterm": "",
            "hostname": "somewhere-else",
            "data": "",
        }
    )


async def _attach(pymux: Pymux, detach_others: bool = False) -> ServerConnection:
    "One client, the way both real transports make one."
    server_end, client_end = connect_in_memory()
    connection = ServerConnection(pymux, server_end)
    pymux.connections.append(connection)

    client_end.write_nowait(_start_gui(detach_others))

    with anyio.fail_after(5.0):
        while connection.client_state is None and not connection._closed:
            await anyio.sleep(0.005)

    return connection


async def test_the_client_that_asked_stays():
    """
    A connection is in the list from the moment the server accepts it,
    which is before its `start-gui` arrives. So a loop over the list
    reaches the client that asked, and `attach -d` used to put that
    person back at their shell.
    """
    pymux = _server()

    async with pymux.running():
        second = await _attach(pymux, detach_others=True)

        assert not second._closed, "attach -d detached the client that asked"
        assert second.client_state is not None

        second.detach_and_close()
        pymux.stop()


async def test_the_other_clients_go():
    pymux = _server()

    async with pymux.running():
        first = await _attach(pymux)
        assert not first._closed

        second = await _attach(pymux, detach_others=True)

        assert first._closed, "attach -d left the other client attached"
        assert not second._closed

        second.detach_and_close()
        pymux.stop()


async def test_without_the_flag_nobody_goes():
    pymux = _server()

    async with pymux.running():
        first = await _attach(pymux)
        second = await _attach(pymux)

        assert not first._closed
        assert not second._closed

        first.detach_and_close()
        second.detach_and_close()
        pymux.stop()
