"""
kitty's file transfer, OSC 5113: a pane's session goes to a client whose
terminal transfers files, and the terminal's answers come back to that
pane. Lillecarl/pymux#912.
"""

from __future__ import annotations

import sys

from libpymux.protocol import Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from pyte.sequences.file_transfer import Action, TransferCommand
from session import once, over_connection

from pymux.core import kitty_transfers
from pymux.options.options import ALL_CLIENT_OPTIONS

SIZE = Size(rows=24, columns=80)
PANE_THAT_STAYS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

SEND = TransferCommand(Action.SEND, id="s1")
OK = TransferCommand(Action.STATUS, id="s1", status="OK")


class Wired:
    "What the terminal of one client was sent, and what the pane's program was told."

    def __init__(self, state, pane) -> None:
        self.terminal: list[str] = []
        self.program: list[str] = []
        state.client.write_output_raw = self.terminal.append
        state.client.flush_output = lambda: None
        pane.screen.write_process_input = self.program.append

    def told(self) -> list[TransferCommand]:
        prefix = "\x1b]5113;"
        return [TransferCommand.decode(text[len(prefix) : -2]) for text in self.program if text.startswith(prefix)]


async def _attached(session, version: str):
    state, _ = await session.attach("one", SIZE)
    state.client.graphics.terminal_version = version
    with set_app(state.app):
        await session.pymux.panes.create_window(PANE_THAT_STAYS)
    pane = session.pymux.arrangement.get_active_window().panes[0]
    return state, pane, Wired(state, pane)


def _program_says(pane, command: TransferCommand) -> None:
    "The command as the pane's screen reads it."
    pane.screen.osc("5113", command.param())


async def test_a_session_goes_to_kitty_and_its_answers_come_back():
    async with over_connection() as session:
        state, pane, wired = await _attached(session, "kitty(0.49.2)")
        _program_says(pane, SEND)
        assert wired.terminal == [str(SEND)]

        state.client.connection._process({"cmd": "in", "data": str(OK)})
        assert wired.program == [str(OK)], "as the terminal wrote it"

        data = TransferCommand(Action.DATA, id="s1", file_id="f1", data=b"abc")
        _program_says(pane, data)
        assert wired.terminal[-1] == str(data)


async def test_an_answer_for_no_session_reaches_nobody():
    async with over_connection() as session:
        state, _pane, wired = await _attached(session, "kitty(0.49.2)")
        state.client.connection._process({"cmd": "in", "data": str(OK)})
        assert wired.program == []


async def test_a_kitty_that_lets_a_remote_program_skip_its_question_gets_none():
    "kitty 0.49.2 fixed it: a program in a pane is remote to kitty."
    async with over_connection() as session:
        _state, pane, wired = await _attached(session, "kitty(0.49.1)")
        _program_says(pane, SEND)
        assert wired.terminal == []
        [refused] = wired.told()
        assert refused.action is Action.STATUS and refused.status.startswith("EPERM:")


async def test_a_silent_session_is_refused_silently():
    async with over_connection() as session:
        _state, pane, wired = await _attached(session, "iTerm2 3.6.4")
        _program_says(pane, TransferCommand(Action.SEND, id="s1", quiet=2))
        assert wired.told() == []


async def test_passthrough_sends_it_to_any_terminal():
    async with over_connection() as session:
        state, pane, wired = await _attached(session, "iTerm2 3.6.4")
        ALL_CLIENT_OPTIONS["file-transfer"].set_value(session.pymux, "passthrough", target=state)
        _program_says(pane, SEND)
        assert wired.terminal == [str(SEND)]


async def test_the_program_hears_when_the_terminal_goes():
    async with over_connection() as session:
        state, pane, wired = await _attached(session, "kitty(0.49.2)")
        _program_says(pane, SEND)
        session.pymux.remove_client(state.client.connection)
        [gone] = wired.told()
        assert gone.status.startswith("EIO:")


async def _sent_more_than_the_pane_holds(tmp_path, program: str, monkeypatch) -> tuple[list[str], Wired]:
    "The terminal sends past `HOLD_BYTES` to a pane running `program`, and the client hears what the server says."
    heard: list[str] = []
    script = tmp_path / "program.py"
    script.write_text(program)
    monkeypatch.setattr(kitty_transfers, "STALL_SECONDS", 0.5)

    def read_packet(packet: dict) -> None:
        if packet["cmd"] in (Packet.HOLD_INPUT, Packet.RELEASE_INPUT):
            heard.append(packet["cmd"])

    async with over_connection(read_packet=read_packet) as session:
        state, _ = await session.attach("one", SIZE)
        state.client.graphics.terminal_version = "kitty(0.49.2)"
        with set_app(state.app):
            await session.pymux.panes.create_window("%s %s" % (sys.executable, script))
        pane = session.pymux.arrangement.get_active_window().panes[0]
        write = pane.screen.write_process_input
        wired = Wired(state, pane)
        pane.screen.write_process_input = write
        _program_says(pane, SEND)
        data = TransferCommand(Action.DATA, id="s1", file_id="f1", data=b"x" * (2 * kitty_transfers.HOLD_BYTES))
        state.client.connection._process({"cmd": "in", "data": str(data)})
        await once(lambda: Packet.RELEASE_INPUT in heard, 20.0, lambda: "never released; heard %r" % (heard,))
    return heard, wired


async def test_a_terminal_waits_while_the_pane_reads_what_it_sent(tmp_path, monkeypatch):
    "Lillecarl/pymux#915."
    heard, wired = await _sent_more_than_the_pane_holds(
        tmp_path,
        "import os, time, tty\ntty.setraw(0)\ntime.sleep(0.2)\nwhile os.read(0, 65536):\n    pass\n",
        monkeypatch,
    )
    assert heard[:2] == [Packet.HOLD_INPUT, Packet.RELEASE_INPUT]
    assert str(TransferCommand(Action.CANCEL, id="s1")) not in wired.terminal


async def test_a_program_that_reads_nothing_loses_its_session_and_the_terminal_goes_on(tmp_path, monkeypatch):
    # Raw: a canonical line past 4095 bytes is thrown away, and the pty drains.
    heard, wired = await _sent_more_than_the_pane_holds(
        tmp_path, "import time, tty\ntty.setraw(0)\ntime.sleep(30)\n", monkeypatch
    )
    assert heard[:2] == [Packet.HOLD_INPUT, Packet.RELEASE_INPUT]
    assert wired.terminal[-1] == str(TransferCommand(Action.CANCEL, id="s1"))


async def test_the_terminal_drops_the_session_of_a_program_that_ended():
    async with over_connection() as session:
        _state, pane, wired = await _attached(session, "kitty(0.49.2)")
        _program_says(pane, SEND)
        session.pymux.kitty_transfers.forget(pane)
        assert wired.terminal[-1] == str(TransferCommand(Action.CANCEL, id="s1"))
