"""
Tab in the command bar, the way a person presses it.

These go through the real buffer and the real key: the text is typed,
Tab is fed to the key processor, and what the buffer holds afterwards
is the answer -- the line it completed to, or the menu it opened.
`test_target_completion.py` and `test_parser_tree.py` ask the
completer itself; this is what reaches the screen.
"""

from __future__ import annotations

import sys

import anyio
from prompt_toolkit.application.current import set_app
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from session import create_session

WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


class Tabbed:
    """
    What one Tab left: the line, and the words its menu offers, if it
    opened one.

    A word is what the line ends with once that entry is taken. The
    first Tab writes in what every entry starts with -- `o` for `off`
    and `on` -- so an entry's own text is only the rest of it.
    """

    def __init__(self, text: str, offered: list[str], said: dict[str, str]) -> None:
        self.text = text
        self.offered = offered
        self.said = said


async def tab(state, line: str) -> Tabbed:
    buffer = state.command_buffer
    with set_app(state.app):
        state.app.layout.focus(buffer)
        buffer.text = line
        buffer.cursor_position = len(line)
        state.app.key_processor.feed(KeyPress(Keys.ControlI))
        state.app.key_processor.process_keys()
    # The completion runs as a task of the buffer. One alone is inserted
    # at once, and then no menu ever opens.
    with anyio.move_on_after(2):
        while buffer.complete_state is None and buffer.text == line:
            await anyio.sleep(0.01)
    found = buffer.complete_state
    completions = found.completions if found is not None else []
    before = buffer.text[: buffer.cursor_position]
    words = [(before[: len(before) + c.start_position] + c.text).rsplit(" ", 1)[-1] for c in completions]
    return Tabbed(
        buffer.text,
        words,
        {word: c.display_meta_text for word, c in zip(words, completions, strict=True)},
    )


async def test_an_option_name_comes_before_a_flag_its_help_matches():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "set-option -g sta")

    assert found.offered[0] == "status"
    assert "-g" not in found.offered


async def test_a_flag_already_given_is_not_offered_again():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "set-option -g ")

    assert "-g" not in found.offered
    assert "status" in found.offered
    assert found.offered.index("status") < min(
        (found.offered.index(word) for word in found.offered if word.startswith("-")), default=len(found.offered)
    )


async def test_an_option_value_follows_its_name():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "set-option -g status ")

    assert found.offered[:2] == ["off", "on"]


async def test_set_option_offers_the_server_s_names_too():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "set-option ")

    assert "status" in found.offered
    assert "set-clipboard" in found.offered


async def test_s_offers_only_the_server_s_names():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "set-option -s ")

    assert "set-clipboard" in found.offered
    assert "status" not in found.offered


async def test_a_server_option_s_values_follow_its_name():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "set-option set-clipboard ")

    assert "external" in found.offered


async def test_a_window_target_opens_a_menu_of_every_window():
    async with create_session(window=WAITS) as (pymux, state):
        await pymux.panes.create_window(WAITS)
        found = await tab(state, "select-window -t ")
        session = pymux.sessions[0].name
        windows = ["%s:%s" % (session, window.index) for window in pymux.sessions[0].arrangement.windows]

    assert found.offered == windows
    # What every entry starts with, written in by the first Tab.
    assert found.text == "select-window -t %s:" % session


async def test_a_single_answer_is_written_in_at_once():
    async with create_session(window=WAITS) as (pymux, state):
        await pymux.panes.create_window(WAITS, name="editor")
        found = await tab(state, "select-window -t edit")
        editor = pymux.sessions[0].arrangement.windows[-1]

    assert found.text == "select-window -t %s:%s" % (pymux.sessions[0].name, editor.index)


async def test_a_session_switch_offers_the_sessions():
    async with create_session(window=WAITS) as (pymux, state):
        found = await tab(state, "move-window -S ")
        names = [session.name for session in pymux.sessions]

    # One session is a single answer, written in.
    assert found.text == "move-window -S %s" % names[0] if len(names) == 1 else set(names) <= set(found.offered)


async def test_a_key_completes_its_chord():
    async with create_session(window=WAITS) as (_pymux, state):
        found = await tab(state, "bind-key ctrl+hom")

    assert found.text == "bind-key ctrl+home"
