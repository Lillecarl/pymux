"""
A pymux client whose terminal is a pyte screen: what a browser is shown
when it is shown the session and not one pane.

A pane stream cannot show what pymux draws over a pane. A client draws
it, so `SessionScreen` is a client, attached the way a terminal's is,
and these read back what it drew. Lillecarl/pymux#481.

The pane runs the shell that a first client's session gets, because
that is the session a person attaching to a new server gets too.
"""

import asyncio
import contextvars
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import pytest

from pymux.log import logger
from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from libpymux.protocol import Packet
from pymux.server import ServerConnection
from pymux.web.session import SessionScreen

from session import once

ROWS, COLUMNS = 12, 60

#: What copy mode draws in the corner of the pane: "[line/lines]".
COPY_POSITION = re.compile(r"\[\d+/\d+\]")


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


@asynccontextmanager
async def attached(pymux, read_only: bool = False):
    """
    A `SessionScreen` on a connection of its own, with a task feeding it
    everything the server sends. The context and the list of connections
    are what both real transports give a connection.

    **The client end closes before the server stops.** Left open, the
    server's read of it never ends and `Pymux.running` waits on it for
    ever: measured, a run that never finished.
    """
    server_end, client_end = connect_in_memory()
    context = contextvars.copy_context()
    connection = context.run(lambda: ServerConnection(pymux, server_end))
    pymux.connections.append(connection)

    session = SessionScreen(
        ROWS,
        COLUMNS,
        lambda packet: client_end.write_nowait(json.dumps(packet)),
        read_only=read_only,
    )

    async def feed() -> None:
        while True:
            try:
                packet = await client_end.read()
            except Exception:
                return
            if session.take_packet(json.loads(packet)):
                return

    feeding = asyncio.get_running_loop().create_task(feed())

    # What the server complained of, for a failure to print.
    logged: list = []

    class _Keep(logging.Handler):
        def emit(self, record) -> None:
            logged.append(self.format(record))

    keep = _Keep(logging.WARNING)
    logger.addHandler(keep)
    session.logged = logged

    session.start()
    try:
        await once(lambda: connection.client_state, 10.0, "no client was made")
        yield session
    finally:
        logger.removeHandler(keep)
        client_end.close()
        feeding.cancel()
        pymux.stop()


def rows_of(session: SessionScreen) -> list:
    screen = session.screen
    top = screen.line_offset
    return [
        line.text for line in screen.page.text_lines(top, top + screen.lines - 1)
    ]


async def shows(session: SessionScreen, wanted, seconds: float = 10.0) -> None:
    """
    Wait until the screen holds `wanted`, a string or a pattern.

    The complaint is written after the wait and not before it: written
    first, it described the screen at the start and blamed a server that
    had not been given a turn yet.
    """
    def there() -> bool:
        text = "\n".join(rows_of(session))
        if isinstance(wanted, str):
            return wanted in text
        return wanted.search(text) is not None

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if there():
            return
        await asyncio.sleep(0.01)
    pytest.fail(
        "the session never showed %r\nthe server said: %s\nit showed:\n%s"
        % (wanted, "\n".join(session.logged) or "nothing", "\n".join(rows_of(session)))
    )


def said(session: SessionScreen, **message) -> None:
    assert session.take(json.dumps(message)) is None


async def test_it_shows_the_pane_and_the_status_line(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        # A pane stream has no status line; a client draws one.
        assert rows_of(session)[-1].strip()


async def test_what_the_viewer_types_reaches_the_program(pymux):
    """The answer is on the screen and the question is not: 42 is output."""
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")

        said(session, type="text", text="echo $((6*7))")
        said(session, type="input", keys="Enter")
        await shows(session, re.compile(r"^42\s*$", re.MULTILINE))


async def test_copy_mode_is_drawn(pymux):
    """
    The overlay a pane stream cannot show. The prefix and `[` are what a
    person types, so the keys go the way a person's do.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        assert not COPY_POSITION.search("\n".join(rows_of(session)))

        said(session, type="input", keys="C-b [")
        await shows(session, COPY_POSITION)


async def test_a_frame_carries_what_it_drew(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")

        assert session.welcome()["size"] == {"columns": COLUMNS, "rows": ROWS}
        drawn = "".join(
            text
            for runs in session.frame()["rows"].values()
            for _style, text in runs
        )
        assert "$" in drawn


async def test_a_read_only_client_refuses_keys(pymux):
    async with pymux.running(), attached(pymux, read_only=True) as session:
        await shows(session, "$")

        refused = session.take(json.dumps({"type": "input", "keys": "Enter"}))
        assert refused == "this client only watches"


async def test_a_resize_reaches_the_server(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")

        session.resize(20, 70)
        connection = pymux.connections[-1]
        await once(
            lambda: (connection.size.rows, connection.size.columns) == (20, 70),
            10.0,
            "the server never took the new size",
        )


def test_a_dangling_sequence_does_not_swallow_the_next_output():
    """
    The parser here is the outer terminal, and a program can leave a
    sequence open. Without the ground timer the viewer would show the
    wrong screen for ever. Lillecarl/pymux#485.
    """
    session = SessionScreen(ROWS, COLUMNS, lambda packet: None)
    session.take_packet({"cmd": Packet.OUT, "data": "abc\x1b[1;"})
    assert session._stream.ground_timer_active

    session._ground_timer.since -= 5  # The timeout passed with no byte.
    session.take_packet({"cmd": Packet.OUT, "data": "def"})

    assert not session._stream.ground_timer_active
    assert rows_of(session)[0].startswith("abcdef")
