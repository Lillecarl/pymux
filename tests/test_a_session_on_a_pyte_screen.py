"""
A pymux client whose terminal is a pyte screen: what a browser is shown
when it is shown the session and not one pane.

A pane stream cannot show what pymux draws over a pane. A client draws
it, so `SessionScreen` is a client, attached the way a terminal's is,
and these read back what it drew. Lillecarl/pymux#481.

The pane runs the shell that a first client's session gets, because
that is the session a person attaching to a new server gets too.
"""

from __future__ import annotations

import contextvars
import json
import logging
import random
import re
import sys
import time
from contextlib import asynccontextmanager

import anyio
import pytest
from libpymux.protocol import Packet
from prompt_toolkit.application.current import set_app
from session import once

from pymux.log import logger
from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection
from pymux.web.session import SessionScreen

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

    # What the server complained of, for a failure to print.
    logged: list = []

    class _Keep(logging.Handler):
        def emit(self, record) -> None:
            logged.append(self.format(record))

    keep = _Keep(logging.WARNING)
    logger.addHandler(keep)
    session.logged = logged

    session.start()
    async with anyio.create_task_group() as tg:
        tg.start_soon(feed)
        try:
            await once(lambda: connection.client_state, 10.0, "no client was made")
            yield session
        finally:
            logger.removeHandler(keep)
            client_end.close()
            tg.cancel_scope.cancel()
            pymux.stop()


def rows_of(session: SessionScreen) -> list:
    screen = session.screen
    top = screen.line_offset
    return [line.text for line in screen.page.text_lines(top, top + screen.lines - 1)]


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
        await anyio.sleep(0.01)
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
        drawn = "".join(text for runs in session.frame()["rows"].values() for _style, text in runs)
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


#: A program that writes the three ways a busy pane scrolls, and then
#: says so: lines that slide the main screen, a block it rewrites in
#: place the way ink does, and a region scrolled on the other page.
FLOOD = r"""
import sys, time
w = sys.stdout.write
for n in range(120):
    w("\x1b[3%dmline %d of the flood\x1b[m\n" % (n % 7 + 1, n))
for frame in range(40):
    w("\x1b[?2026h")
    w("\x1b[3A" if frame else "")
    for k in range(3):
        w("\x1b[2K\x1b[1m⏺ status %d.%d ✳ ●\x1b[m\n" % (frame, k))
    if frame % 4 == 0:
        w("\x1b]8;;https://example.com\x1b\\kept %d \U0001f600\x1b]8;;\x1b\\\n" % frame)
    w("\x1b[?2026l")
    sys.stdout.flush()
    time.sleep(0.005)
w("\x1b[?1049h\x1b[2;8r\x1b[8;1H")
for n in range(60):
    w("\nregion %d" % n)
w("\x1b[r\x1b[?1049l")
w("DONE-FLOOD\n")
sys.stdout.flush()
time.sleep(1000)
"""


def wire_differs(session: SessionScreen, client_state) -> list[str]:
    """
    Every cell where the client's terminal and the frame pymux committed
    disagree. The committed frame is what the renderer diffs the next
    frame against, so a cell that differs here stays wrong on the
    terminal until something redraws it.
    """
    committed = client_state.app.renderer._last_screen
    outer = session.screen
    found = []
    for y in range(ROWS):
        shown = outer.data_buffer[outer.line_offset + y]
        meant = committed.data_buffer[y]
        for x in range(COLUMNS):
            if shown[x].char != meant[x].char:
                found.append("row %d column %d: terminal %r, frame %r" % (y, x, shown[x].char, meant[x].char))
    return found


async def settled(session: SessionScreen, seconds: float = 0.3) -> None:
    "Until the client's screen has not changed for `seconds`."
    last = None
    still_since = time.monotonic()
    while time.monotonic() - still_since < seconds:
        now = rows_of(session)
        if now != last:
            last = now
            still_since = time.monotonic()
        await anyio.sleep(0.02)


async def test_the_terminal_holds_the_frame_beside_a_scrolling_pane(pymux, tmp_path):
    """
    The line between two panes stays whole while one of them scrolls.

    A scroll on the wire moves whole rows of the terminal, the other
    pane and the line between them too, and a row the renderer
    believes it drew has to be on the terminal. So after a pane beside
    another has scrolled every way a busy program does, every cell of
    the client's terminal is the cell of the frame pymux committed.
    """
    flood = tmp_path / "flood.py"
    flood.write_text(FLOOD)

    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        said(session, type="input", keys="C-b %")
        await settled(session)
        said(session, type="text", text="%s %s" % (sys.executable, flood))
        said(session, type="input", keys="Enter")
        await shows(session, "DONE-FLOOD")
        await settled(session)

        client_state = pymux.connections[-1].client_state
        found = wire_differs(session, client_state)
        assert not found, "\n".join([*found[:20], "", *rows_of(session)])

        # And the frame itself holds the line, on every row of the
        # panes: a frame that lost it agrees with a terminal that lost
        # it, and the comparison above would pass.
        committed = client_state.app.renderer._last_screen

        def drawn_line(char: str) -> bool:
            return "─" <= char <= "╿"

        line = [x for x in range(COLUMNS) if drawn_line(committed.data_buffer[ROWS // 2][x].char)]
        assert len(line) == 1, rows_of(session)
        broken = [y for y in range(1, ROWS - 1) if not drawn_line(committed.data_buffer[y][line[0]].char)]
        assert not broken, "\n".join(["the line is cut on rows %s" % broken, *rows_of(session)])


def line_breaks(session: SessionScreen, client_state) -> list[str]:
    """
    The rows where the line between two side by side panes is missing,
    in the frame pymux committed or on the client's terminal.
    """
    committed = client_state.app.renderer._last_screen
    outer = session.screen

    def drawn_line(char: str) -> bool:
        return "─" <= char <= "╿"

    # The column most pane rows draw a line in. A program may print a
    # line character of its own, so one row cannot say which it is.
    pane_rows = range(1, ROWS - 1)
    column = max(
        range(COLUMNS),
        key=lambda x: sum(drawn_line(committed.data_buffer[y][x].char) for y in pane_rows),
    )
    found = []
    for y in pane_rows:
        if not drawn_line(committed.data_buffer[y][column].char):
            found.append("frame row %d column %d: %r" % (y, column, committed.data_buffer[y][column].char))
        if not drawn_line(outer.data_buffer[outer.line_offset + y][column].char):
            found.append(
                "terminal row %d column %d: %r" % (y, column, outer.data_buffer[outer.line_offset + y][column].char)
            )
    return found


@pytest.mark.parametrize(
    "switches",
    [("select-pane -L", "select-pane -R"), ("select-pane -t :.-", "select-pane -t :.+")],
    ids=["by-direction", "by-order"],
)
@pytest.mark.parametrize("painted", [False, True], ids=["plain", "painted"])
async def test_switching_panes_keeps_the_line_between_them(pymux, painted, switches):
    """
    The line between two panes changes weight with the active pane, and
    it is whole after every switch, in the frame and on the terminal.

    Painted is a theme with `paint-screen` on, so every cell carries a
    background and a switch repaints both panes whole.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            if painted:
                pymux.handle_command("set-option paint-screen on")
                pymux.handle_command("set-option pane-border-status on")
                pymux.handle_command("set-client-option theme pygments:catppuccin-mocha")
            pymux.handle_command("split-window -h 'sleep 1000'")
        await settled(session)
        assert not line_breaks(session, client_state), "\n".join(rows_of(session))

        for step in range(6):
            command = switches[step % 2]
            with set_app(client_state.app):
                pymux.handle_command(command)
            await settled(session)
            found = line_breaks(session, client_state) + wire_differs(session, client_state)
            assert not found, "\n".join(["after %s, switch %d" % (command, step), *found, "", *rows_of(session)])


async def test_redraw_writes_what_the_terminal_lost(pymux):
    """
    A frame writes only what changed, so a cell the terminal lost stays
    lost. `redraw` writes every cell again. The terminal here loses the
    line between two panes, the way a real one did.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            pymux.handle_command("split-window -h 'sleep 1000'")
        await settled(session)

        outer = session.screen
        for y in range(1, ROWS - 1):
            row = outer.data_buffer[outer.line_offset + y]
            for x in list(row):
                if "─" <= row[x].char <= "╿":
                    del row[x]
        assert line_breaks(session, client_state)

        with set_app(client_state.app):
            pymux.handle_command("redraw")
        await settled(session)

        found = line_breaks(session, client_state) + wire_differs(session, client_state)
        assert not found, "\n".join([*found, "", *rows_of(session)])


async def test_which_key_leaves_nothing_behind(pymux):
    """
    The popup the prefix opens covers the pane, and when it goes the
    pane is on the terminal again, every cell of it.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            pymux.handle_command("set-option which-key on")
        said(session, type="text", text="echo " + "row " * 12)
        said(session, type="input", keys="Enter")
        await settled(session)

        said(session, type="input", keys="C-b")
        await settled(session, 1.0)
        found = wire_differs(session, client_state)
        assert "break-pane" in "\n".join(rows_of(session)), rows_of(session)
        assert not found, "\n".join(["with the popup up", *found, "", *rows_of(session)])

        # A bound key ends the prefix, and the popup with it. The prefix
        # twice sends one to the pane, which bash takes as a move left.
        said(session, type="input", keys="C-b")
        await settled(session, 1.0)
        assert "break-pane" not in "\n".join(rows_of(session)), rows_of(session)
        found = wire_differs(session, client_state)
        assert not found, "\n".join(["after the popup went", *found, "", *rows_of(session)])


#: Characters a busy program writes: plain, wide, ambiguous, combining.
FUZZ_TEXT = ["a", "bc", "def ", "  ", "⏺", "✳", "●", "中", "\U0001f600", "é", "│"]


def fuzz_chunk(rnd: random.Random, rows: int, columns: int) -> str:
    "One piece of what a program writes, chosen from the ways it moves the screen."
    kind = rnd.randrange(14)
    if kind < 4:
        sgr = rnd.choice(["", "\x1b[1m", "\x1b[3%dm" % rnd.randrange(8), "\x1b[4%dm" % rnd.randrange(8), "\x1b[7m"])
        return sgr + "".join(rnd.choice(FUZZ_TEXT) for _ in range(rnd.randrange(1, 12))) + "\x1b[m"
    if kind == 4:
        return "\r\n" * rnd.randrange(1, 4)
    if kind == 5:
        return "\x1b[%d;%dH" % (rnd.randrange(1, rows + 1), rnd.randrange(1, columns + 1))
    if kind == 6:
        return "\x1b[%dK" % rnd.randrange(3)
    if kind == 7:
        return "\x1b[%dJ" % rnd.randrange(3)
    if kind == 8:
        top = rnd.randrange(1, rows)
        bottom = rnd.randrange(top + 1, rows + 1)
        return "\x1b[%d;%dr\x1b[%d;1H" % (top, bottom, bottom) + "\n" * rnd.randrange(1, 4) + "\x1b[r"
    if kind == 9:
        return "\x1b[%d%s" % (rnd.randrange(1, 4), rnd.choice("STLM@P"))
    if kind == 10:
        return rnd.choice(["\x1b[?1049h", "\x1b[?1049l"])
    if kind == 11:
        return "\x1b[%dX" % rnd.randrange(1, 6)
    if kind == 12:
        return "\t" * rnd.randrange(1, 3)
    return "\x1b[%d%s" % (rnd.randrange(1, 5), rnd.choice("ABCD"))


@pytest.mark.parametrize("mode", ["optimized", "reference"])
@pytest.mark.parametrize("seed", range(12))
async def test_every_frame_beside_a_pane_is_on_the_terminal(pymux, seed, mode):
    """
    Random output in a pane beside another, a frame at a time.

    After each piece the client's terminal holds the frame pymux
    committed, cell for cell, and the line between the panes is
    whole. The renderer skips cursor moves, batches gaps, trims rows
    and scrolls regions on the wire, and each of those is a way for
    the terminal and the frame to part. The pieces are what programs
    write; the seed makes a failure repeatable.
    """
    rnd = random.Random(seed)
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            pymux.handle_command("set-option render-mode %s" % mode)
            pymux.handle_command("split-window -h 'sleep 1000'")
        await settled(session)
        controls = [
            pane.terminal.terminal_control for pane in sorted(pymux.panes_by_id.values(), key=lambda pane: pane.pane_id)
        ]

        for step in range(60):
            kind = rnd.randrange(10)
            if kind == 0:
                chunk = rnd.choice(["select-pane -t :.-", "select-pane -t :.+"])
                with set_app(client_state.app):
                    pymux.handle_command(chunk)
            elif kind == 1:
                chunk = "resize-pane -%s %d" % (rnd.choice("LR"), rnd.randrange(1, 6))
                with set_app(client_state.app):
                    pymux.handle_command(chunk)
            else:
                control = rnd.choice(controls)
                chunk = fuzz_chunk(rnd, control.screen.lines, control.screen.columns)
                control.feed_output(chunk)
            await settled(session, 0.05)

            found = wire_differs(session, client_state)
            assert not found, "\n".join(
                ["seed %d, step %d, after %r" % (seed, step, chunk), *found[:20], "", *rows_of(session)]
            )
            found = line_breaks(session, client_state)
            assert not found, "\n".join(
                ["seed %d, step %d, after %r" % (seed, step, chunk), *found, "", *rows_of(session)]
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
