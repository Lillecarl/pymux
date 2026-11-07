"""
An agent's message, on a row under its pane's bar. Lillecarl/pymux#931.

Carl: "show the 'long status' on a new line in the pane titlebar".
`pane-message-row` says where the row comes from: `take` makes the
program one row shorter, `cover` draws over its first row.

**The pane's height is judged twice over two frames.** The plan and the
drawing both tell the pty its size, and the row is taken in both or the
pty hears two heights in turn.
"""

from __future__ import annotations

import pytest
from test_strip_draws import CHROME, create_client, dump

#: "Approve edit?" in base64, as OSC 7501 carries a message.
APPROVE = "QXBwcm92ZSBlZGl0Pw"
SHOWN = "blocked: Approve edit?"


def _pane(pymux):
    return pymux.arrangement.get_active_window().active_pane


def _first_terminal_row(pymux, rows, above=0):
    "The first row the program draws on, or a row above it."
    position = pymux.get_client_state().layout_manager.pane_write_positions[_pane(pymux)]
    return rows[position.ypos - above]


async def test_take_gives_the_message_a_row_of_the_pane():
    async with create_client(CHROME) as (pymux, draw):
        pane = _pane(pymux)
        draw()
        height = pane.process.sy
        pane.screen.osc("7501", "state=blocked:msg=%s" % APPROVE)
        for _ in range(2):
            rows = draw()
            assert SHOWN in _first_terminal_row(pymux, rows, above=1), dump(rows)
            assert SHOWN not in _first_terminal_row(pymux, rows), dump(rows)
            assert pane.process.sy == height - 1
        pane.screen.osc("7501", "state=clear")
        draw()
        assert pane.process.sy == height


async def test_cover_draws_over_the_pane_and_keeps_its_size():
    async with create_client([*CHROME, "set-window-option pane-message-row cover"]) as (pymux, draw):
        pane = _pane(pymux)
        draw()
        height = pane.process.sy
        pane.screen.osc("7501", "state=blocked:msg=%s" % APPROVE)
        rows = draw()
        assert SHOWN in _first_terminal_row(pymux, rows), dump(rows)
        assert pane.process.sy == height


@pytest.mark.parametrize("record", ["state=blocked", "state=working:msg=%s" % APPROVE])
async def test_no_row_without_a_message_or_with_the_row_off(record):
    "No message, no row; and `off` shows none even with one."
    commands = [*CHROME] if "msg" not in record else [*CHROME, "set-window-option pane-message-row off"]
    async with create_client(commands) as (pymux, draw):
        pane = _pane(pymux)
        draw()
        height = pane.process.sy
        pane.screen.osc("7501", record)
        rows = draw()
        assert "Approve edit?" not in "".join(rows.values()), dump(rows)
        assert pane.process.sy == height
