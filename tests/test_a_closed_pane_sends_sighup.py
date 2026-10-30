"""
A pane a person closes sends its program SIGHUP, and SIGKILL only after
a grace.

SIGKILL alone gave a shell no chance to write its history, so the line
that closed the pane never reached it: `pymux wl` in a pane, which
turns the pane into the window, was lost from `~/.bash_history`. The
signals themselves are judged in `ptyhost`; this judges that each
command that closes a pane asks for SIGHUP.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import in_this_process, once

from pymux.commands import handle_command
from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)

#: Says it is ready once the trap is set, and writes "handled" from it.
TRAPS = "trap 'echo handled > %(handled)s; exit 0' HUP\ntouch %(ready)s\nwhile :; do sleep 0.05; done\n"


async def _command(pymux, state, text):
    "Run a command as the person at this client, and wait for it."
    with set_app(state.app):
        answer = handle_command(pymux, text)
        if answer is not None:
            await answer


def _trapping(tmp_path):
    "A script that traps SIGHUP, and the two files it writes."
    ready, handled = tmp_path / "ready", tmp_path / "handled"
    script = tmp_path / "traps.sh"
    script.write_text(TRAPS % {"ready": ready, "handled": handled})
    return "sh %s" % (script,), ready, handled


async def test_kill_pane_lets_the_program_run_its_sighup_trap(tmp_path):
    command, ready, handled = _trapping(tmp_path)
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        await _command(pymux, state, "split-window '%s'" % (command,))
        pane = pymux.arrangement.get_active_pane()
        await once(ready.exists, 5.0, "the script never set its trap")

        await _command(pymux, state, "kill-pane -t %%%s" % (pane.pane_id,))

        await once(handled.exists, 5.0, "the program never ran its SIGHUP trap")
        assert handled.read_text() == "handled\n"


async def test_kill_server_lets_every_program_run_its_sighup_trap(tmp_path):
    "And then stops. Lillecarl/pymux#679."
    command, ready, handled = _trapping(tmp_path)
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        await _command(pymux, state, "split-window '%s'" % (command,))
        await once(ready.exists, 5.0, "the script never set its trap")

        await _command(pymux, state, "kill-server")

        assert handled.read_text() == "handled\n"
        assert pymux.done.is_set()


async def test_the_last_kill_session_lets_the_program_run_its_sighup_trap(tmp_path):
    "It stops the server, which SIGKILLed what kill-session had just hung up."
    command, ready, handled = _trapping(tmp_path)
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        await _command(pymux, state, "split-window '%s'" % (command,))
        await once(ready.exists, 5.0, "the script never set its trap")

        await _command(pymux, state, "kill-session")

        await once(pymux.done.is_set, 10.0, "the server never stopped")
        assert handled.read_text() == "handled\n"


async def test_respawn_pane_k_lets_the_program_run_its_sighup_trap(tmp_path):
    command, ready, handled = _trapping(tmp_path)
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        await _command(pymux, state, "split-window '%s'" % (command,))
        await once(ready.exists, 5.0, "the script never set its trap")

        await _command(pymux, state, "respawn-pane -k 'sleep 30'")

        await once(handled.exists, 5.0, "the program never ran its SIGHUP trap")
        assert handled.read_text() == "handled\n"
