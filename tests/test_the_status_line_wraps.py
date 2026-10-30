"""
A window list wider than its row wraps onto more rows.

A phone is forty columns, and six windows do not fit beside the
session name and the clock. Cut, the windows past the edge are not
there to read or click, so the status line takes more rows instead:
an entry moves to the next row whole, up to `status-max-rows`, and the
panes and everything drawn above the status line move up with it.
Lillecarl/pymux#723.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from session import in_this_process

from pymux.options.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS

PHONE = Size(rows=20, columns=40)
DESK = Size(rows=20, columns=200)

NOTHING = "%s -c pass" % ("python3",)

#: Names long enough that six of them overflow forty columns.
NAMES = ["editor", "server", "logs", "tests", "database", "notes"]


async def with_windows(session, size, names=NAMES):
    "A client of that size on a session holding one window per name."
    pymux = session.pymux
    state, _ = await session.attach("phone", size)
    with set_app(state.app):
        pymux.arrangement.get_active_window().chosen_name = names[0]
        for name in names[1:]:
            await pymux.create_window(NOTHING, name=name)
    return state


def set_option(pymux, state, name, value):
    table = ALL_CLIENT_OPTIONS if name in ALL_CLIENT_OPTIONS else ALL_OPTIONS
    with set_app(state.app):
        table[name].set_value(pymux, value)


def drawn(state) -> list[str]:
    "The rows of one frame of this client, as text."
    size = state.app.output.get_size()
    screen = Screen(initial_width=size.columns)
    manager = state.layout_manager
    with set_app(state.app):
        manager.frame_started(state.app)
        try:
            state.app.layout.container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=size.columns, height=size.rows),
                parent_style="",
                erase_bg=False,
                z_index=None,
            )
            screen.draw_all_floats()
        finally:
            manager.frame_ended(state.app)
    return ["".join(screen.data_buffer[y][x].char for x in range(size.columns)).rstrip() for y in range(size.rows)]


async def test_a_frame_formats_each_side_of_the_status_line_once():
    """
    The room for the window list, the status line and `what_time_moves`
    after the frame all read both sides, and a side may be a template.
    Lillecarl/pymux#824.
    """
    async with in_this_process() as session:
        state = await with_windows(session, DESK)
        set_option(session.pymux, state, "status-left", "left-#{session_name}")
        set_option(session.pymux, state, "status-right", "right-#{window_index}")
        status = state.layout_manager.status
        formatted: list[str] = []
        format_one = status._format

        def counted(string: str) -> str:
            formatted.append(string)
            return format_one(string)

        status._format = counted
        manager = state.layout_manager
        size = state.app.output.get_size()
        with set_app(state.app):
            manager.frame_started(state.app)
            try:
                state.app.layout.container.write_to_screen(
                    Screen(initial_width=size.columns),
                    MouseHandlers(),
                    WritePosition(xpos=0, ypos=0, width=size.columns, height=size.rows),
                    parent_style="",
                    erase_bg=False,
                    z_index=None,
                )
                manager.what_time_moves()
            finally:
                manager.frame_ended(state.app)

        assert formatted.count("left-#{session_name}") == 1, formatted
        assert formatted.count("right-#{window_index}") == 1, formatted

        # Between frames each read is fresh: the clock asks there.
        status.right()
        assert formatted.count("right-#{window_index}") == 2, formatted


async def test_windows_that_fit_take_one_row():
    async with in_this_process() as session:
        state = await with_windows(session, DESK)

        assert state.layout_manager.status.rows() == 1
        assert session.pymux.planes.size().rows == DESK.rows - 1


async def test_windows_that_do_not_fit_take_more_rows():
    async with in_this_process() as session:
        state = await with_windows(session, PHONE)

        rows = state.layout_manager.status.rows()
        assert rows > 1
        assert state.layout_manager.room_this_client_has.rows == PHONE.rows - rows
        assert session.pymux.planes.size().rows == PHONE.rows - rows


async def test_every_window_is_drawn_whole():
    "No name is cut at the edge or broken across two rows."
    async with in_this_process() as session:
        state = await with_windows(session, PHONE)
        rows = state.layout_manager.status.rows()

        status = drawn(state)[-rows:]

        for name in NAMES:
            assert sum(name in row for row in status) == 1, (name, status)


async def test_the_session_name_and_the_clock_keep_the_first_row():
    async with in_this_process() as session:
        state = await with_windows(session, PHONE)
        rows = state.layout_manager.status.rows()

        first, *rest = drawn(state)[-rows:]

        assert first.startswith("[")
        assert not any(row.startswith("[") for row in rest)


async def test_status_max_rows_caps_it():
    async with in_this_process() as session:
        state = await with_windows(session, PHONE)
        set_option(session.pymux, state, "status-max-rows", "1")

        assert state.layout_manager.status.rows() == 1
        assert session.pymux.planes.size().rows == PHONE.rows - 1


async def test_full_screen_takes_none():
    async with in_this_process() as session:
        state = await with_windows(session, PHONE)
        set_option(session.pymux, state, "full-screen", "on")

        assert state.layout_manager.status.rows() == 0
        assert session.pymux.planes.size().rows == PHONE.rows


async def test_a_message_sits_on_the_row_above_the_status_line():
    "Anchored to the bottom of the body, it moves up with the status line."
    async with in_this_process() as session:
        state = await with_windows(session, PHONE)
        rows = state.layout_manager.status.rows()
        state.message = "a message for the person"

        screen = drawn(state)

        assert "a message for the person" in screen[-rows - 1]


async def test_the_wide_client_sizes_nothing_for_the_narrow_one():
    """
    The tallest status line of anybody watching comes off the plane.
    The wide client draws background in the rows it does not need.
    """
    async with in_this_process() as session:
        phone = await with_windows(session, PHONE)
        await session.attach("desk", Size(rows=PHONE.rows, columns=DESK.columns))

        assert session.pymux.planes.size().rows == PHONE.rows - phone.layout_manager.status.rows()


async def test_the_plane_is_the_same_whichever_client_asks():
    """
    A render of one client asks the plane, and the plane asks every
    watcher's status line. Each one is formatted as that watcher sees
    it, or the plane would change with the client that rendered last
    and resize every program on each alternation.
    """
    async with in_this_process() as session:
        phone = await with_windows(session, PHONE)
        desk, _ = await session.attach("desk", Size(rows=PHONE.rows, columns=DESK.columns))

        with set_app(phone.app):
            from_the_phone = session.pymux.planes.size()
        with set_app(desk.app):
            from_the_desk = session.pymux.planes.size()

        assert from_the_phone == from_the_desk
