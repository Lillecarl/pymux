"""
`send-keys -X previous-prompt` and `next-prompt` move copy mode between
the rows a shell marked with "OSC 133", as tmux's do. Lillecarl/pymux#500.
"""

from __future__ import annotations

import anyio
from prompt_toolkit.application.current import set_app
from pyte.page import RowMark
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture

#: Two prompts, and the output of a command between them, each marked
#: the way a shell's integration marks them.
MARKED = r"printf '\033]133;A\007first\n\033]133;C\007out\n\033]133;A\007second\n'"


def _marked_rows(pane, mark: RowMark) -> int:
    return sum(1 for row in pane.screen.page.data_buffer.values() if row.marks & mark)


async def test_copy_mode_jumps_between_the_marked_rows(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        shell = pymux.arrangement.get_active_pane()
        shell.process.write_input(MARKED + "\r")
        with anyio.fail_after(10):
            while _marked_rows(shell, RowMark.PROMPT) < 2:
                await anyio.sleep(0.02)

        with set_app(pymux.connections[-1].client_state.app):
            pymux.handle_command("copy-mode")
            view = shell.terminal.copy_view
            view.buffer.cursor_position = len(view.buffer.text)

            def caret_line() -> str:
                return view.buffer.document.current_line

            pymux.handle_command("send-keys -X previous-prompt")
            assert caret_line() == "second"
            pymux.handle_command("send-keys -X previous-prompt")
            assert caret_line() == "first"
            pymux.handle_command("send-keys -X next-prompt -o")
            assert caret_line() == "out"
