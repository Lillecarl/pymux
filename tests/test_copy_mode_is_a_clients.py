"""
Copy mode belongs to the client that opens it.

Two people can watch one pane. One of them reading its history must not
stop the program for the other, and must not move the other's view or
take the other's keys. Each client is an application of its own, and
`ptterm` keeps a copy mode for each. Lillecarl/pymux#470.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.format import format_pymux_string

SIZE = Size(rows=24, columns=80)

#: A program that answers: what is typed comes back on the screen.
ECHOES = "cat"


def _the_pane(pymux):
    return pymux.arrangement.get_active_window().active_pane


def _shown(pane) -> str:
    return "\n".join("".join(cell.char for cell in row.values()) for row in pane.screen.page.data_buffer.values())


async def test_one_client_reads_the_history_and_the_other_stays_live():
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window(ECHOES)
        reading, _ = await session.attach("reading", SIZE)
        watching, _ = await session.attach("watching", SIZE)
        pane = _the_pane(pymux)

        with set_app(reading.app):
            pymux.handle_command("copy-mode")

        assert pane.is_copying_for(reading.app)
        assert not pane.is_copying_for(watching.app)
        assert not pane.process.suspended

        # The watcher types, and the program answers on the screen.
        session.typed(watching, "still here\r")
        await once(lambda: "still here" in _shown(pane), 5.0, "the program stopped for copy mode")

        # What the reader reads is the copy it opened.
        view = pane.terminal.copy_views.of(reading.app)
        assert "still here" not in view.buffer.text


async def test_the_reader_leaves_and_the_watcher_never_entered():
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window(ECHOES)
        reading, _ = await session.attach("reading", SIZE)
        watching, _ = await session.attach("watching", SIZE)
        pane = _the_pane(pymux)

        with set_app(reading.app):
            pymux.handle_command("copy-mode")
        session.typed(reading, "q")

        await once(lambda: not pane.is_copying_for(reading.app), 5.0, "q never left copy mode")
        assert not pane.is_copying_for(watching.app)


async def test_two_clients_read_one_history_apart():
    "Each has a caret of its own, and one moving it does not move the other's."
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window(ECHOES)
        first, _ = await session.attach("first", SIZE)
        second, _ = await session.attach("second", SIZE)
        pane = _the_pane(pymux)
        pane.terminal.terminal_control.stream.feed("one\r\ntwo\r\nthree")

        for state in (first, second):
            with set_app(state.app):
                pymux.handle_command("copy-mode")
        views = [pane.terminal.copy_views.of(state.app) for state in (first, second)]
        assert views[0] is not views[1]
        caret = views[1].buffer.cursor_position

        session.typed(first, "\x1b[A")
        await once(lambda: views[0].buffer.cursor_position != caret, 5.0, "the first client could not move")
        assert views[1].buffer.cursor_position == caret


async def test_the_mode_of_a_pane_is_the_clients():
    "`#{pane_mode}` names copy mode for the client reading, and nothing for the one watching."
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window(ECHOES)
        reading, _ = await session.attach("reading", SIZE)
        watching, _ = await session.attach("watching", SIZE)
        pane = _the_pane(pymux)

        with set_app(reading.app):
            pymux.handle_command("copy-mode")

        for state, expected in ((reading, "copy-mode"), (watching, "")):
            with set_app(state.app):
                assert format_pymux_string(pymux, "#{pane_mode}", pane=pane, client=state) == expected
                assert format_pymux_string(pymux, "#{pane_mode}", pane=pane) == expected


async def test_a_pane_killed_while_a_client_reads_it():
    "The reader is left on the pane that remains, and nothing raises."
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window(ECHOES)
        reading, _ = await session.attach("reading", SIZE)
        watching, _ = await session.attach("watching", SIZE)
        window = pymux.arrangement.get_active_window()
        with set_app(watching.app):
            pymux.handle_command("split-window %s" % ECHOES)
        await once(lambda: len(window.panes) == 2, 5.0, "the split never came")
        pane = window.active_pane

        with set_app(reading.app):
            pymux.handle_command("copy-mode")
        assert pane.is_copying_for(reading.app)
        with set_app(watching.app):
            pymux.handle_command("kill-pane")
        await once(lambda: len(window.panes) == 1, 5.0, "the pane was never killed")

        (left,) = window.panes
        with set_app(reading.app):
            reading.sync_focus()
            assert reading.app.layout.has_focus(left.terminal)
        session.typed(reading, "after\r")
        await once(lambda: "after" in _shown(left), 5.0, "the reader lost the keyboard")


async def test_the_copy_holds_rows_of_the_kind_the_screen_holds():
    """
    With the Rust kernel, a row is `pyte_rs.Row`, and the copy of a row
    of the screen is one too: `CopyView.freeze` copies through the
    row's own type.
    """
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window(ECHOES)
        state, _ = await session.attach("reading", SIZE)
        pane = _the_pane(pymux)
        pane.terminal.terminal_control.stream.feed("on the screen")

        with set_app(state.app):
            pymux.handle_command("copy-mode")
        screen = pane.screen
        live = screen.page.data_buffer[screen.line_offset]
        kept = pane.terminal.copy_views.of(state.app).page.data_buffer[screen.line_offset]

        assert kept is not live
        assert type(kept) is type(live)
        assert kept.image() == live.image()
        # pymux requires the kernel, so this is the row a person meets.
        assert type(live).__module__ == "pyte_rs"

        # And what the program writes after does not reach it.
        # `ptterm/tests/test_a_copy_never_changes.py` runs every change
        # over the rows in Python; these are the ones that write a row
        # of the screen in place.
        view = pane.terminal.copy_views.of(state.app)
        before = {number: row.image() for number, row in view.page.data_buffer.items()}
        stream = pane.terminal.terminal_control.stream
        stream.feed("\x1b[H" + "x" * 30 + "\x1b[2K" + "\x1b[2;4r\x1b[3S\x1b[r" + "\x1b[H\x1b[2L\x1b[3M")
        assert {number: row.image() for number, row in view.page.data_buffer.items()} == before
