"""
What "detach-client" does: it closes the client's connection, and the
session goes on. Lillecarl/pymux#160.
"""

from __future__ import annotations

import io
import sys

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from session import Connection

from pymux.main import Pymux

ROWS, COLUMNS = 24, 80


async def test_client_over_connection_still_detaches():
    "A client has a connection to close, and closing it is not ending the session."
    detached = []

    class _Connection(Connection):
        "The one that records the detach, which is what the test reads."

        def detach_and_close(self, hang_up: bool = False):
            detached.append(True)

    pymux = Pymux()
    async with pymux.running():
        await pymux.create_window("%s -c pass" % (sys.executable,))
        output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS))
        with create_pipe_input() as pipe:
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=_Connection(),
            )
            try:
                with set_app(state.app):
                    pymux.handle_command("detach-client")

                assert detached == [True]
                assert not pymux.done.is_set()
            finally:
                pymux.stop()
