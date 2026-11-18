"""
`set-window-option -t` and `set-option -t` name the window a window
option is set on, as tmux's -t does for an option of that scope.
tmux's own example configuration writes `setw -t0:1 ...`.
Lillecarl/pyterm#1035.
"""

from __future__ import annotations

from session import in_this_process
from test_sessions import SIZE, WAITING, _command, _server


async def two_windows(session):
    pymux = session.pymux
    state, _ = await session.attach("only", SIZE)
    await _command(pymux, state, "new-window '%s'" % (WAITING,))
    first, second = pymux.arrangement.windows
    return pymux, state, first, second


async def test_setw_t_sets_the_named_window_only():
    async with in_this_process(_server()) as session:
        pymux, state, first, second = await two_windows(session)
        await _command(pymux, state, "setw -t0:%d window-status-separator ' | '" % (first.index,))
        assert first.options.window_status_separator == " | "
        assert second.options.window_status_separator == " "


async def test_set_t_names_a_window_for_a_window_option():
    async with in_this_process(_server()) as session:
        pymux, state, first, second = await two_windows(session)
        await _command(pymux, state, "set -t :%d window-status-separator ' | '" % (second.index,))
        assert second.options.window_status_separator == " | "
        assert first.options.window_status_separator == " "
