"""
Everything a client's terminal said, kept and shown. Lillecarl/pymux#725.

A terminal nobody documents -- a phone's -- could only be guessed at:
pymux asked a dozen questions, acted on a few answers and dropped the
rest. Now every reply is kept per client, and `show-terminal` reads
them back.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.terminal_answers import SURVEY, TerminalAnswers, readable

SIZE = Size(rows=24, columns=80)
DA2 = "\x1b[>1;10;0c"
MOUSE_MODE = "\x1b[?1000;2$y"
STATUS = "\x1b[0n"


def frames_of(packets: list) -> str:
    return "".join(packet["data"] for packet in packets if packet.get("cmd") == "out" and packet.get("data"))


async def test_every_reply_is_kept_with_a_count():
    async with over_connection() as session:
        state, _ = await session.attach("phone", SIZE)
        said = state.connection.terminal_said

        session.typed(state, DA2)
        session.typed(state, MOUSE_MODE)
        session.typed(state, STATUS)
        session.typed(state, STATUS)
        await once(lambda: len(said) == 3, 5.0, "the replies were not kept")

        counts = {answer.text: answer.count for answer in said}
        assert counts == {DA2: 1, MOUSE_MODE: 1, STATUS: 2}


async def test_the_secondary_attributes_never_reach_a_pane():
    async with over_connection() as session:
        state, _ = await session.attach("phone", SIZE)
        pane = session.pymux.arrangement.get_active_pane()
        written: list = []
        pane.process.write_input = written.append

        session.typed(state, DA2)
        await once(lambda: len(state.connection.terminal_said) == 1, 5.0, "the reply was not read")

        assert "".join(written) == ""


async def test_the_survey_goes_out_with_the_detection():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state, _ = await session.attach("phone", SIZE)

        state.connection._process({"cmd": "kitty-detect"})

        await once(lambda: SURVEY in frames_of(packets), 5.0, "the survey was not asked")


async def test_show_terminal_lists_them():
    async with over_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("phone", SIZE)
        session.typed(state, DA2)
        session.typed(state, STATUS)
        session.typed(state, STATUS)
        said = state.connection.terminal_said
        await once(lambda: sum(answer.count for answer in said) == 3, 5.0, "the replies were not kept")

        pymux.command_output = []
        pymux.handle_command("show-terminal")
        out, pymux.command_output = "\n".join(pymux.command_output), None

        assert "term xterm-256color" in out
        assert "\\e[>1;10;0c  x1" in out
        assert "\\e[0n  x2" in out


def test_the_store_is_capped():
    answers = TerminalAnswers()
    for number in range(1000):
        answers.heard("\x1b[%dR" % number)

    assert len(answers) == 256
    assert next(iter(answers)).text == "\x1b[744R"


def test_readable_writes_out_control_characters():
    assert readable("\x1b]11;rgb:0/0/0\x07") == "\\e]11;rgb:0/0/0\\a"
    assert readable("\x1bP>|Termius 7.11.2\x1b\\") == "\\eP>|Termius 7.11.2\\e\\"
