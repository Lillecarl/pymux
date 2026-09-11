"""
`compose-key` sends a key this keyboard cannot type.

Carl's words on Lillecarl/pymux#220: "claude code wants me to press
`ctrl+home` to scroll down to the bottom, I have a macbook without a
home key and I don't want to learn this machine's stupid keymap
always."

A laptop with no Home key, no Insert and no function row cannot answer
a program that asks for one, and the fn chords differ per machine and
per external keyboard. pymux is the layer in the middle, and
`send-keys` could do it all along -- once Lillecarl/pymux#234 made the
key nameable. What was missing is a way to reach it without knowing the
spelling.

So this is a prompt with the key names on it. What the tests below read
is which container the layout would draw, which is the filter of each
float, and what reaches the pane when the answer is accepted.
"""

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.document import Document
from prompt_toolkit.layout.containers import ConditionalContainer, Float

from session import a_session, in_a_loop
from pymux.commands.commands import call_command_handler
from pymux.key_spelling import PREFIX, KeyCompleter


def the_float_of(state, name: str) -> Float:
    "The float that draws the box `name` builds."
    for one in state.layout_manager.layout.floats:
        inner = getattr(one.content, "content", None)
        builder = getattr(inner, "get_container", None)
        if builder is not None and builder.__name__ == name:
            return one
    raise AssertionError("the layout draws no %s" % (name,))


def is_drawn(state, wanted) -> bool:
    """
    Whether the layout would draw this now.

    A float or a container inside one. What decides is the
    `ConditionalContainer` in it, so unwrapping the float is the first
    step and reading the filter is the second.
    """
    if isinstance(wanted, Float):
        wanted = wanted.content
    with set_app(state.app):
        if isinstance(wanted, ConditionalContainer):
            return bool(wanted.filter())
        return True


def the_bottom_prompt(state) -> ConditionalContainer:
    "The one row version of the prompt, along the bottom."
    manager = state.layout_manager
    window = manager._prompt_window()
    for one in state.layout_manager.layout.floats:
        for child in getattr(one.content, "children", []):
            if isinstance(child, ConditionalContainer) and child.content is window:
                return child
    raise AssertionError("the layout draws no prompt along the bottom")


def compose(pymux, state, *arguments):
    "Run `compose-key`, the way a binding would."
    with set_app(state.app):
        call_command_handler("compose-key", pymux, list(arguments))


def leave(pymux, state):
    "Close the prompt without answering it, the way Escape does."
    with set_app(state.app):
        pymux.leave_command_mode()


def answer(pymux, state, text: str):
    "Type an answer into the prompt and accept it."
    written = []
    pane = pymux.arrangement.get_active_window().active_pane
    pane.process.write_input = written.append
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    with set_app(state.app):
        state.prompt_buffer.set_document(Document(text), bypass_readonly=True)
        state.prompt_buffer.validate_and_handle()
    return "".join(written), errors


# ----------------------------------------------------------------------
# The box.


@in_a_loop
async def test_it_asks_in_a_box():
    """
    The completions are what need the room. A menu hanging off a bottom
    row stops at twelve, and in the box it takes the height of the box.
    """
    async with a_session() as (pymux, state):
        compose(pymux, state)

        assert is_drawn(state, the_float_of(state, "_key_box"))


@in_a_loop
async def test_nothing_is_drawn_until_it_is_asked_for():
    async with a_session() as (pymux, state):
        assert not is_drawn(state, the_float_of(state, "_key_box"))


@in_a_loop
async def test_an_ordinary_prompt_still_uses_the_bottom_row():
    """
    A question with no answers to offer has nothing to put in a box, so
    `command-prompt` is where it was. Only a question that knows what
    the answers are gets the room.
    """
    async with a_session() as (pymux, state):
        with set_app(state.app):
            call_command_handler(
                "command-prompt", pymux, ["-p", "Name", "rename-window %%"]
            )

        assert not is_drawn(state, the_float_of(state, "_key_box"))
        assert is_drawn(state, the_bottom_prompt(state))


@in_a_loop
async def test_the_box_replaces_the_bottom_row_and_does_not_join_it():
    "One question, asked once."
    async with a_session() as (pymux, state):
        compose(pymux, state)

        assert not is_drawn(state, the_bottom_prompt(state))


@in_a_loop
async def test_the_menu_under_the_cursor_steps_aside_for_the_box():
    "The box holds a menu of its own. Two at once would be one too many."
    async with a_session() as (pymux, state):
        compose(pymux, state)

        cursor_menu = next(
            one for one in state.layout_manager.layout.floats if one.xcursor
        )
        assert not is_drawn(state, cursor_menu)


@in_a_loop
async def test_the_prompt_window_is_built_once():
    """
    The layout focuses a control. A fresh one on every render is one it
    never focused, and then the cursor is drawn nowhere and a person
    cannot see where they are typing.
    """
    async with a_session() as (pymux, state):
        manager = state.layout_manager

        assert manager._prompt_window() is manager._prompt_window()
        assert manager._key_box() is manager._key_box()


@in_a_loop
async def test_the_box_says_what_it_is_asking_for_once():
    """
    The title row says "Send key", so the line under it says nothing.
    Saying it twice is a row wasted and a thing to read.
    """
    async with a_session() as (pymux, state):
        manager = state.layout_manager

        compose(pymux, state)
        assert manager._before_prompt_command_tokens() == []

        leave(pymux, state)
        state.prompt_text = "Name"
        assert manager._before_prompt_command_tokens() != []


# ----------------------------------------------------------------------
# What it sends.


@pytest.mark.parametrize(
    "written,expected",
    [
        ("ctrl+home", "\x1b[1;5H"),
        ("C-Home", "\x1b[1;5H"),
        ("Home", "\x1b[H"),
        ("f5", "\x1b[15~"),
        ("ctrl+shift+end", "\x1b[1;6F"),
        # A sequence: two presses, in order.
        ("escape a", "\x1ba"),
    ],
)
@in_a_loop
async def test_the_key_reaches_the_pane(written, expected):
    async with a_session() as (pymux, state):
        compose(pymux, state)

        assert answer(pymux, state, written) == (expected, [])


@in_a_loop
async def test_it_closes_when_the_key_has_gone():
    "The pane gets the focus back, and the box is not still open."
    async with a_session() as (pymux, state):
        compose(pymux, state)

        answer(pymux, state, "ctrl+home")

        assert state.prompt_completer is None
        assert not is_drawn(state, the_float_of(state, "_key_box"))


@in_a_loop
async def test_leaving_it_puts_the_completer_back():
    """
    The completer says the prompt draws in a box, so one left behind
    would put the next question in one.
    """
    async with a_session() as (pymux, state):
        compose(pymux, state)
        assert state.prompt_completer is not None

        leave(pymux, state)

        assert state.prompt_completer is None


@in_a_loop
async def test_a_name_no_key_has_goes_as_text():
    "`send-keys` sends what it cannot read as the text it is."
    async with a_session() as (pymux, state):
        compose(pymux, state)

        assert answer(pymux, state, "notakey") == ("notakey", [])


@in_a_loop
async def test_the_message_can_be_changed():
    async with a_session() as (pymux, state):
        compose(pymux, state, "-p", "Which key")

        assert state.prompt_text == "Which key"


@in_a_loop
async def test_it_can_start_with_something_written():
    async with a_session() as (pymux, state):
        compose(pymux, state, "-I", "ctrl+")

        assert state.prompt_buffer.text == "ctrl+"


# ----------------------------------------------------------------------
# What it offers.


@in_a_loop
async def test_it_offers_the_keys_a_keyboard_leaves_out():
    async with a_session() as (pymux, state):
        compose(pymux, state)

        with set_app(state.app):
            offered = [
                completion.text
                for completion in state.prompt_completer.get_completions(
                    Document("ho"), None
                )
            ]
        assert offered == ["home"]


def test_the_box_does_not_offer_the_prefix():
    """
    The prefix is a step of the grammar, so a box composing a binding
    wants it. This box sends a key to a pane, and the prefix is the one
    key pymux keeps for itself: `send-prefix` is what sends it on.
    """
    offered = [
        completion.text
        for completion in KeyCompleter(offer_the_prefix=False).get_completions(
            Document(""), None
        )
    ]
    assert PREFIX not in offered
    assert offered
