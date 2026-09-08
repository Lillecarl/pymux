"""
Which window a client that has not looked yet is given.

`get_active_window_for` cached one window and returned another: the
cache took `self._last_active_window or self.windows[0]` and the return
took `self.windows[0]`. So the first answer for a new client was
"window one" and every answer after it was "the window the session was
last on". A render drew window one for a frame; a key binding acted on
it.

**A stand-in asks the first question.** A real client asks it inside
`add_client`, while its layout is being built, so by the time a test
holds the client the cache is already filled and the wrong answer has
gone to whoever asked. The map keys on the application and does nothing
else with it, so an object of our own is a client that has not looked
yet, which is the only thing these tests need it to be.

The clients here are real, because `set_active_window` reads `get_app()`
and a session that nobody has looked at has no last active window to
land on.

Lillecarl/pymux#193.
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


class _NotLookedYet:
    """
    A client that has never asked which window is active.

    `_active_window_for_cli` is a `WeakKeyDictionary` keyed by the
    application, and `get_active_window_for` uses the key and nothing
    else, so this is enough to be one.
    """


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
async def a_server(windows):
    """
    A server with this many windows, and a way to attach a client.

    Attaching the first client is what gives a server with no window
    one, so the count is reached by topping up rather than by creating
    every window here.
    """
    pymux = Pymux()
    pipes = []

    def attach():
        "One more client, and the application it draws with."
        output = Vt100_Output(
            stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
        )
        pipe = create_pipe_input()
        pipes.append(pipe)
        return pymux.add_client(
            output=output,
            input=pipe.__enter__(),
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=_Connection(),
        ).app

    try:
        first = attach()
        with set_app(first):
            while len(pymux.arrangement.windows) < windows:
                pymux.create_window(NOTHING)
        assert len(pymux.arrangement.windows) == windows

        yield pymux, attach
    finally:
        for window in list(pymux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()
        for pipe in pipes:
            pipe.__exit__(None, None, None)


@in_a_loop
async def test_a_new_client_lands_where_the_session_is():
    "It landed on window one, once, and on the last active one after."
    async with a_server(3) as (pymux, _):
        arrangement = pymux.arrangement
        arrangement.set_active_window(arrangement.windows[2])

        assert (
            arrangement.get_active_window_for(_NotLookedYet()) is arrangement.windows[2]
        )


@in_a_loop
async def test_the_first_answer_is_the_one_it_keeps():
    "The bug itself: the cache and the return took different windows."
    async with a_server(3) as (pymux, _):
        arrangement = pymux.arrangement
        arrangement.set_active_window(arrangement.windows[2])
        arriving = _NotLookedYet()

        first_answer = arrangement.get_active_window_for(arriving)

        assert arrangement.get_active_window_for(arriving) is first_answer


@in_a_loop
async def test_with_nowhere_to_land_it_takes_the_first_window():
    "Nothing has been made active, so there is no last active window."
    async with a_server(3) as (pymux, _):
        arrangement = pymux.arrangement
        arrangement._last_active_window = None

        assert (
            arrangement.get_active_window_for(_NotLookedYet()) is arrangement.windows[0]
        )


@in_a_loop
async def test_a_window_that_is_gone_is_not_offered():
    """
    A program can end the last pane of a window that no client is on.
    `remove_pane` moves a client off a window it empties, and there is
    no client to move, so the window leaves the list while
    `_last_active_window` still names it.

    Both answers are read. The window that is gone was what the cache
    took, so it came back on the second question and not on the first.
    """
    async with a_server(3) as (pymux, _):
        arrangement = pymux.arrangement
        gone = arrangement.windows[2]
        arrangement.set_active_window(gone)
        arrangement.windows.remove(gone)
        arriving = _NotLookedYet()

        assert arrangement.get_active_window_for(arriving) is arrangement.windows[0]
        assert arrangement.get_active_window_for(arriving) is arrangement.windows[0]


@in_a_loop
async def test_a_client_that_has_looked_keeps_its_own_window():
    "Two clients on two windows. Neither answer moves the other."
    async with a_server(3) as (pymux, attach):
        arrangement = pymux.arrangement
        first = attach()
        with set_app(first):
            arrangement.set_active_window(arrangement.windows[0])

        second = attach()
        with set_app(second):
            arrangement.set_active_window(arrangement.windows[2])

        assert arrangement.get_active_window_for(first) is arrangement.windows[0]
        assert arrangement.get_active_window_for(second) is arrangement.windows[2]
