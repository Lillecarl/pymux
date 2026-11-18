"""
`window-status-separator` is what the status line writes between two
windows, as tmux's status-format puts it after every window but the
last. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import in_this_process
from test_the_status_line_wraps import DESK, PHONE, drawn, with_windows


def separate(pymux, state, separator: str) -> None:
    "Each open window, the way `set -w` sets one: `-g` reaches only a new window."
    with set_app(state.app):
        for window in pymux.arrangement.windows:
            window.options.window_status_separator = separator


async def test_the_separator_goes_between_windows():
    async with in_this_process() as session:
        state = await with_windows(session, DESK, names=["one", "two", "three"])
        separate(session.pymux, state, " | ")

        status = drawn(state)[-1]

        assert "1:one  | 2:two- | 3:three* " in status, status
        assert "3:three* |" not in status, status


async def test_a_wider_separator_wraps_sooner():
    async with in_this_process() as session:
        state = await with_windows(session, PHONE, names=["a", "b", "c"])
        assert state.layout_manager.status.rows() == 1
        separate(session.pymux, state, "  ~~~  ")

        assert state.layout_manager.status.rows() == 2
