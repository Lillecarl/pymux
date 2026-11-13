"""
A command packet the server cannot read is answered, never left waiting.
Lillecarl/pyterm#984.
"""

from __future__ import annotations

import anyio
from libpymux.wire import encode_json

from pymux.connection.server import ServerConnection
from pymux.core.main import Pymux
from pymux.pipes.memory import connect_in_memory

DEADLINE = 5


async def test_a_pane_field_that_is_not_a_number_is_refused():
    "libpymux once sent `%1001` here, and every caller hung."
    pymux = Pymux()
    pymux.options.test_mode = True

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json({"cmd": "run-command", "data": "has-session", "pane_id": "%1"}))

        said = []
        with anyio.fail_after(DEADLINE):
            while not said or said[-1]["cmd"] != "exit":
                said.append(await client_end.read())

        pymux.stop()

    assert said[-1]["code"] == 1
    assert [p["data"] for p in said if p["cmd"] == "err"] == ["pymux: not a pane number: '%1'\n"]
