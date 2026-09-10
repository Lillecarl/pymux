"""
Walking the layout twice finds the same controls.

prompt_toolkit keys the key bindings of the whole application by the
set of controls it can reach: `_CombinedRegistry._key_bindings` builds
`(current_window, frozenset(other_controls))` and looks that up in a
cache of eight. A container that hands back a fresh object each walk is
a key that never matches, so every key press rebuilt every binding.

`checks.pymux-keystroke` holds the total, and a total says a stage grew
without saying why. This says which invariant the number rests on.
Lillecarl/pymux#233.
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


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None


def in_a_loop(test):
    "Run this test in an event loop of its own."

    @functools.wraps(test)
    def run(*arguments, **named):
        asyncio.run(test(*arguments, **named))

    return run


@asynccontextmanager
async def a_session():
    "A server with one client and one window."
    pymux = Pymux()
    pymux.create_window("%s -c pass" % (sys.executable,))

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
            yield pymux, state
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def the_controls(app):
    "What `_CombinedRegistry` would key its cache by."
    return frozenset(app.layout.find_all_controls())


@in_a_loop
async def test_walking_the_layout_twice_finds_the_same_controls():
    async with a_session() as (pymux, state):
        with set_app(state.app):
            first = the_controls(state.app)
            second = the_controls(state.app)

    assert first == second


@in_a_loop
async def test_the_empty_overlay_is_one_window_and_not_a_new_one():
    """
    The container that held a fresh `Window()`, which makes a
    `DummyControl` of its own every time it is asked.
    """
    async with a_session() as (pymux, state):
        manager = state.layout_manager
        with set_app(state.app):
            assert pymux.overlay_pane is None
            assert manager._overlay_container() is manager._overlay_container()
