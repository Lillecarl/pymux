"""
"OSC 6", iTerm2's tab colour: a tab is a window, so the status line
draws a window's name in the colour of its active pane.
Lillecarl/pymux#786.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from pyte.osc import Osc
from session import in_this_process

SIZE = Size(rows=24, columns=80)


async def test_the_window_takes_the_tab_colour_of_its_active_pane():
    async with in_this_process() as session:
        state, _ = await session.attach("one", SIZE)
        window = state.session.arrangement.get_active_window()

        def styles():
            return [style for style, *_ in state.layout_manager.status.window_list() if "window" in style]

        assert not any("bg:#" in style for style in styles())

        window.active_pane.screen.osc(Osc.TAB_COLOR, "1;bg;red;brightness;255")
        assert any(style.endswith(" bg:#ff0000 fg:#ffffff") for style in styles())

        window.active_pane.screen.osc(Osc.TAB_COLOR, "1;bg;green;brightness;255")
        assert any(style.endswith(" bg:#ffff00 fg:#000000") for style in styles())

        window.active_pane.screen.osc(Osc.TAB_COLOR, "1;bg;*;default")
        assert not any("bg:#" in style for style in styles())
