"""
Where "new-window" puts the window it opens.

A new window took the lowest free index from `base-index` up, whatever
window a person was looking at. On a session numbered one, two, five,
standing on five, `ctrl+b c` gave window three: a window at the far
end of the map a person had built, which they then had to go and find.

With no gaps the two rules agree, which is why this went so long
without being noticed. The lowest free index is the one after the last
window.

**The tests hold a client.** Which window is active is a fact about a
client, so `Arrangement.get_active_window` reads `get_app()`, and a
test with no application asks about a client that is not there and is
answered with the first window.

Lillecarl/pymux#191.
"""

import asyncio
import functools
import io
import sys
from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux

ROWS, COLUMNS = 24, 80

#: A pane that ends at once and holds a real screen while it lives.
NOTHING = "%s -c pass" % (sys.executable,)


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


def in_a_loop(test):
    "pymux carries no anyio, so pytest here runs no coroutine test."

    @functools.wraps(test)
    def run():
        asyncio.run(test())

    return run


@asynccontextmanager
async def a_session(*indexes):
    """
    A server whose windows carry these indexes, active on the last.

    The windows are made and then numbered, because making one at an
    index is the thing under test and a fixture may not use it.
    """
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=_Connection(),
        )
        try:
            with set_app(state.app):
                # **It counts rather than creates.** A client attaching
                # to a server with no window is given one, so a fixture
                # that made one of its own would have two.
                while len(pymux.arrangement.windows) < len(indexes):
                    pymux.create_window(NOTHING)
                assert len(pymux.arrangement.windows) == len(indexes)

                for window, index in zip(pymux.arrangement.windows, indexes):
                    window.index = index
                pymux.arrangement.windows.sort(key=lambda w: w.index)
                pymux.arrangement.set_active_window(pymux.arrangement.windows[-1])
                yield pymux, state
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def indexes(pymux):
    return [w.index for w in pymux.arrangement.windows]


def run(pymux, command):
    "Run a command the way a key binding or the command line does."
    pymux.handle_command("%s %s" % (command, NOTHING))


@in_a_loop
async def test_a_new_window_lands_next_to_the_one_a_person_is_on():
    "The report: on five of one, two, five, it gave three."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 5, 6]


@in_a_loop
async def test_it_makes_room_when_the_next_index_is_taken():
    async with a_session(1, 2, 3) as (pymux, _):
        pymux.arrangement.set_active_window(pymux.arrangement.windows[0])

        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 3, 4]
        assert pymux.arrangement.get_active_window().index == 2


@in_a_loop
async def test_a_session_with_no_gaps_is_what_it_always_was():
    "Which is why nobody saw this for so long."
    async with a_session(1, 2, 3) as (pymux, _):
        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 3, 4]


@in_a_loop
async def test_only_the_run_that_is_in_the_way_moves():
    "A gap stops the walk, so a window put out of the way stays there."
    async with a_session(1, 2, 3, 7) as (pymux, _):
        pymux.arrangement.set_active_window(pymux.arrangement.windows[0])

        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 3, 4, 7]


@in_a_loop
async def test_before_the_active_window():
    "It takes that window's index, and that window moves up."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -b")

        assert indexes(pymux) == [1, 2, 5, 6]
        assert pymux.arrangement.get_active_window().index == 5


@in_a_loop
async def test_after_a_window_that_is_not_the_active_one():
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -a -t 1")

        assert indexes(pymux) == [1, 2, 3, 5]
        assert pymux.arrangement.get_active_window().index == 2


@in_a_loop
async def test_a_bare_target_names_the_index_to_open_at():
    "Which is how tmux reads one for this command."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -t 9")

        assert indexes(pymux) == [1, 2, 5, 9]


@in_a_loop
async def test_a_target_nobody_can_find_falls_back_to_the_active_window():
    """
    tmux errors there. A person who mistypes a window number while
    opening one does not want the window not to open.
    """
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -a -t nosuchwindow")

        assert indexes(pymux) == [1, 2, 5, 6]


@in_a_loop
async def test_the_window_that_is_reported_is_the_one_that_opened():
    """
    It read the last of the list, which was the new one only while a
    new window always took the highest index.
    """
    async with a_session(1, 2, 3) as (pymux, _):
        pymux.arrangement.set_active_window(pymux.arrangement.windows[0])

        run(pymux, "new-window")

        assert pymux.arrangement.get_active_window().index == 2


@in_a_loop
async def test_dash_d_leaves_the_window_a_person_was_on():
    "And the new one still lands where it would have."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -d")

        assert indexes(pymux) == [1, 2, 5, 6]
        assert pymux.arrangement.get_active_window().index == 5
