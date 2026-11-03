"""
A program in a terminal pane that counts the mouse in pixels (1016)
hears kitty's leave report when the pointer moves off its pane.
Lillecarl/pymux#899.
"""

from __future__ import annotations

import shlex

from prompt_toolkit.data_structures import Size
from pyte.modes import PrivateMode
from run_commands import run_as
from session import once, over_connection

from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)
CELL = (10, 20)


async def test_a_pointer_that_moves_to_another_pane_leaves_the_first(tmp_path):
    heard = tmp_path / "heard"
    program = "stty raw -echo; printf '\\033[?1003h\\033[?1016h'; exec cat > %s" % shlex.quote(str(heard))
    pymux = Pymux()
    async with over_connection(pymux=pymux) as session:
        state, _size = await session.attach("one", SIZE)
        graphics = state.connection.graphics
        graphics.cell_width, graphics.cell_height = CELL
        graphics.cell_size_known = True
        state.connection.replies.pixel_mouse = True
        pymux.session_defaults.mouse = True

        await run_as(pymux, state, "split-window -h %s" % shlex.quote("sh -c %s" % shlex.quote(program)))
        window = state.session.arrangement.get_active_window()
        left, right = window.panes
        screen = right.terminal.terminal_control.screen
        await once(lambda: PrivateMode.SGR_PIXEL_MOUSE.flag in screen.mode, 10.0, "the program never asked for pixels")

        # A property of the last frame: each frame has a table of its own.
        def drawn(pane):
            return state.layout_manager.pane_write_positions.get(pane)

        await once(lambda: drawn(right) and drawn(left), 10.0, "the panes were never drawn")

        def move_to(pane) -> None:
            where = drawn(pane)
            x, y = (where.xpos + 2) * CELL[0] + 5, (where.ypos + 1) * CELL[1] + 10
            state.connection._process({"cmd": "in", "data": "\x1b[<35;%d;%dM" % (x, y)})

        move_to(right)
        await once(lambda: heard.exists() and b"\x1b[<35;" in heard.read_bytes(), 10.0, "no move reached the program")
        move_to(left)
        await once(
            lambda: b"\x1b[<288;" in heard.read_bytes(),
            10.0,
            lambda: "no leave reached the program: %r" % heard.read_bytes(),
        )
