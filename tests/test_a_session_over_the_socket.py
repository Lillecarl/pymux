"""
`run_session` over a real socket: the transport `pymux web` uses for a
session, where `tests/test_a_session_on_a_pyte_screen.py` judges the
screen with no transport at all. Lillecarl/pymux#481.

A socketpair and not a listener: the server end is the same
`PosixSocketConnection` an accepted socket becomes, so the bytes are the
socket route's and no path on disk is needed.
"""

from __future__ import annotations

import contextvars
import json
import re
import socket
import time

import anyio
import pytest
from anyio.abc import UNIXSocketStream

from pymux.main import Pymux
from pymux.pipes.posix import PosixSocketConnection
from pymux.server import ServerConnection
from pymux.web.session import run_session


@pytest.fixture
def pymux():
    mux = Pymux()
    mux.test_mode = True
    try:
        yield mux
    finally:
        for pane in list(mux.panes_by_id.values()):
            if not pane.process.is_terminated:
                pane.process.kill()


class Viewer:
    "What a browser holds: the rows it was sent, and a way to speak."

    def __init__(self) -> None:
        self.frames: list = []
        self.rows: dict = {}
        self.size = None
        self._say, self._said = anyio.create_memory_object_stream(float("inf"))

    async def take(self, frame) -> None:
        self.frames.append(frame)
        if frame.get("whole") or frame["type"] == "welcome":
            self.rows = {}
        if "size" in frame:
            self.size = frame["size"]
        for number, runs in frame.get("rows", {}).items():
            self.rows[int(number)] = "".join(text for _style, text in runs)

    def text(self) -> str:
        return "\n".join(self.rows[number] for number in sorted(self.rows))

    def say(self, **message) -> None:
        self._say.send_nowait(json.dumps(message))

    async def messages(self):
        async for message in self._said:
            yield message

    def done(self) -> None:
        self._say.close()


async def until(question, complaint, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if question():
            return
        await anyio.sleep(0.01)
    pytest.fail(complaint())


async def test_a_session_travels_over_the_socket(pymux):
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    viewer = Viewer()

    async with pymux.running():
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(pymux, PosixSocketConnection(theirs)))
        pymux.connections.append(connection)

        stream = await UNIXSocketStream.from_socket(ours)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_session, stream, viewer.take, viewer.messages(), 12, 60)

            await until(
                lambda: "$" in viewer.text(),
                lambda: "no prompt arrived; the viewer holds:\n" + viewer.text(),
            )
            assert viewer.frames[0]["type"] == "welcome"

            viewer.say(type="text", text="echo $((6*7))")
            viewer.say(type="input", keys="Enter")
            await until(
                lambda: re.search(r"^42\s*$", viewer.text(), re.MULTILINE),
                lambda: "no answer arrived; the viewer holds:\n" + viewer.text(),
            )

            # The viewer's element changed size: the next frame is whole,
            # at the new size.
            viewer.say(type="size", rows=16, columns=70)
            await until(
                lambda: viewer.size == {"columns": 70, "rows": 16},
                lambda: "the size never came back; it is %r" % (viewer.size,),
            )

            viewer.done()
            await stream.aclose()

        pymux.stop()
