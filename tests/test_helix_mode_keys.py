"""
`set-option mode-keys helix`: Helix's keys in copy mode, through ptterm
and ptkhx. Lillecarl/pymux#749.

`ptterm/tests/test_copy_mode_helix.py` judges the keys. This judges that
the option reaches a pane, and that the prefix still comes first.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys
from session import create_session
from test_command_mode import press

from pymux.options.options import ALL_OPTIONS


def _copy_mode_on(pymux, state, written: str):
    "The active pane, with `written` on it, in copy mode with Helix keys."
    ALL_OPTIONS["mode-keys"].set_value(pymux, "helix")
    pane = pymux.arrangement.get_active_window().active_pane
    pane.terminal.terminal_control.stream.feed(written)
    with set_app(state.app):
        pymux.handle_command("copy-mode")
    return pane


async def test_y_copies_what_helix_selected():
    async with create_session() as (pymux, state):
        pane = _copy_mode_on(pymux, state, "hello world")
        press(state, "g")
        press(state, "h")
        press(state, "e")
        press(state, "y")
        assert pane.terminal.copy_views.of(state.app) is None
        assert state.app.clipboard.get_data().text == "hello"


async def test_the_prefix_comes_before_helix():
    "After the prefix `q` is pymux's display-panes, and copy mode stays."
    async with create_session() as (pymux, state):
        pane = _copy_mode_on(pymux, state, "hello")
        press(state, Keys.ControlB)
        press(state, "q")
        assert pane.terminal.copy_views.of(state.app) is not None
