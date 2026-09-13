"""
An integrated server serves commands over its socket, not a terminal.

`pymux integrated` holds the server and its one client in one process.
It binds a socket when one was asked for, so that
`pymux -S <socket> <command>` and libpymux reach it. Nothing of the
user interface reads that socket.

**A second client attaching over it is what made a detach ambiguous.**
The session would belong to two terminals while one of them holds the
process: `ctrl+b d` in that one ends the session for both, whatever the
person meant. So a connection that arrives over the socket of such a
server may run commands and may not attach, and `ctrl+b d` means quit
with nothing to argue about. Lillecarl/pymux#159.
"""

import json
import socket

import anyio

from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import CANNOT_ATTACH, ServerConnection

#: What a client sends to ask for the user interface.
START_GUI = {
    "cmd": "start-gui",
    "detach-others": False,
    "color-depth": None,
    "term": "xterm-256color",
    "colorterm": "",
    "hostname": "somewhere-else",
    "data": "",
}


def _server() -> Pymux:
    pymux = Pymux()
    pymux.test_mode = True
    return pymux


async def _packets(client_end, seconds: float = 2.0) -> list:
    "What the server said, until it said nothing more."
    said = []
    with anyio.move_on_after(seconds):
        while True:
            try:
                said.append(json.loads(await client_end.read()))
            except Exception:
                return said
    return said


async def test_a_client_may_not_attach_when_the_server_serves_one_terminal():
    pymux = _server()

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        connection = ServerConnection(pymux, server_end, may_attach=False)

        client_end.write_nowait(json.dumps(START_GUI))
        said = await _packets(client_end)

        assert connection.client_state is None, "the client attached anyway"
        assert [p["data"] for p in said if p["cmd"] == "out"] == [CANNOT_ATTACH]
        assert connection._closed, "the refused client is still connected"

        pymux.stop()


async def test_a_refused_client_is_told_what_to_leave_with():
    """
    A refusal is not a detach. Both end with the server closing the
    connection, so the code is the only thing that tells a script which
    of the two happened. Lillecarl/pymux#332.
    """
    pymux = _server()

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end, may_attach=False)

        client_end.write_nowait(json.dumps(START_GUI))
        said = await _packets(client_end)

        assert [p["code"] for p in said if p["cmd"] == "exit"] == [1], said

        # And the reason comes first, so a person reads it.
        kinds = [p["cmd"] for p in said]
        assert kinds.index("out") < kinds.index("exit")

        pymux.stop()


def test_a_client_leaves_with_what_the_server_named():
    "The client half of it: an `exit` packet is what sets the code."
    from pymux.client.terminal import TerminalClient

    client = TerminalClient()
    assert client.exit_code == 0

    client._process(json.dumps({"cmd": "exit", "code": 1}).encode("utf-8"))

    assert client.exit_code == 1


async def test_the_reason_reaches_the_client_before_the_close():
    """
    **The write is awaited.** `_send_packet` spawns one into the scope
    of the connection, and closing cancels that scope, so the reason
    would reach the client only if the cancel lost the race. A client
    closed with nothing said leaves a person looking at their shell for
    no reason they can see.
    """
    pymux = _server()

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end, may_attach=False)

        client_end.write_nowait(json.dumps(START_GUI))

        with anyio.fail_after(5.0):
            while True:
                packet = json.loads(await client_end.read())
                if packet["cmd"] == "out":
                    break

        assert "for commands" in packet["data"], packet["data"]

        pymux.stop()


async def test_a_refused_connection_still_runs_a_command():
    "The socket is for commands, so the refusal is about attaching only."
    pymux = _server()

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end, may_attach=False)

        client_end.write_nowait(
            json.dumps({"cmd": "run-command", "data": "has-session", "pane_id": None})
        )
        said = await _packets(client_end, seconds=5.0)

        # A command answers with its output and an exit code, and the
        # connection closes after it. That is what every command over a
        # socket does, and the refusal changed none of it.
        assert [p for p in said if p["cmd"] == "exit"], said

        pymux.stop()


async def test_an_ordinary_server_lets_a_client_attach():
    "The refusal belongs to the integrated route and to no other one."
    pymux = _server()

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        connection = ServerConnection(pymux, server_end)

        client_end.write_nowait(json.dumps(START_GUI))

        with anyio.fail_after(5.0):
            while connection.client_state is None:
                await anyio.sleep(0.005)

        connection.detach_and_close()
        pymux.stop()


async def test_the_socket_of_an_integrated_server_refuses_an_attach(tmp_path):
    """
    The whole way through, over a real socket.

    The route is chosen after the bind: `listen_on_socket` runs before
    `run_integrated` says what this server is. So the question is asked
    when a client arrives, and this is what says so.
    """
    pymux = _server()
    pymux._serves_one_terminal = True
    pymux.listen_on_socket(str(tmp_path / "pymux.sock"))

    async with pymux.running():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(pymux.socket_name)
        client.setblocking(False)
        # The packets of this transport are \0 separated.
        client.send(json.dumps(START_GUI).encode("utf-8") + b"\0")

        got = b""
        with anyio.fail_after(5.0):
            while b"\0" not in got:
                await anyio.wait_readable(client)
                got += client.recv(1024)

        packet = json.loads(got.split(b"\0")[0])
        assert packet["cmd"] == "out"
        assert packet["data"] == CANNOT_ATTACH

        client.close()
        pymux.stop()
