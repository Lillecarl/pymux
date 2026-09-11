"""
Searching the popup that `list-keys` puts up, and repeating the search.

The popup is a read-only `TextArea` with a `SearchToolbar`, and it says
so on its own last row: "Press [q] to quit or [/] for searching." Every
key it promises comes from prompt_toolkit's default bindings, so what
those bindings do decides whether the promise holds.

The tests are coroutines, for the reason `test_command_mode.py` gives:
the key processor starts a background task and asks the running loop
for one. Lillecarl/pymux#214.
"""

from prompt_toolkit.application.current import set_app
from prompt_toolkit.filters import is_searching
from prompt_toolkit.keys import Keys
from session import create_session, in_a_loop
from test_command_mode import press

#: Text that is in the popup more than once, so that there is a second
#: match to move to. Every line of `list-keys` starts with it.
MANY = "bind-key"


def a_popup(pymux, state, vi=False):
    """
    The popup up and focused, the way `list-keys` leaves it.

    `mode-keys` decides the editing mode of everything that is not the
    command line, and that is what gates prompt_toolkit's vi bindings.

    A command reads the client it belongs to off the current
    application, so this has to run as that client.
    """
    with set_app(state.app):
        if vi:
            pymux.handle_command("set-option mode-keys vi")

        pymux.handle_command("list-keys")

    return state.layout_manager._popup_textarea


def searching(state) -> bool:
    with set_app(state.app):
        return bool(is_searching())


def type_a_search(state, text):
    "Open the search, type into it, and accept it."
    press(state, "/")
    for letter in text:
        press(state, letter)
    press(state, Keys.Enter)


# ----------------------------------------------------------------------
# Opening the search.


@in_a_loop
async def test_the_key_the_popup_promises_opens_the_search():
    """
    The popup's own last row says `/` searches. It has to say that in
    whatever editing mode a person is in, because it does not say
    "unless".
    """
    async with create_session() as (pymux, state):
        a_popup(pymux, state)

        press(state, "/")

        assert searching(state)


@in_a_loop
async def test_the_search_opens_with_vi_mode_keys_too():
    async with create_session() as (pymux, state):
        a_popup(pymux, state, vi=True)

        press(state, "/")

        assert searching(state)


# ----------------------------------------------------------------------
# Repeating it.


def in_copy_mode(pymux, state):
    """
    A pane in copy mode, and the buffer that holds its scrollback.

    Copy mode is `ptterm`'s, and it is built from the same read-only
    buffer and `SearchToolbar` as the popup. So the same keys reach it,
    and Lillecarl/pymux#214 says so too.
    """
    with set_app(state.app):
        pymux.handle_command("copy-mode")

    pane = pymux.arrangement.get_active_window().active_pane
    return pane.terminal.copy_buffer


def matches_in(popup):
    "Where every match of the query sits in the popup's text."
    text = popup.buffer.text
    result = []
    at = text.find(MANY)

    while at != -1:
        result.append(at)
        at = text.find(MANY, at + 1)

    return result


@in_a_loop
async def test_accepting_a_search_lands_on_the_first_match():
    "So that the tests below can say which match the cursor is on."
    async with create_session() as (pymux, state):
        popup = a_popup(pymux, state)

        type_a_search(state, MANY)

        assert popup.buffer.cursor_position == matches_in(popup)[0]


@in_a_loop
async def test_a_second_match_is_one_key_away():
    """
    The one Carl asked for. A list of keys is exactly the kind of text
    somebody searches once and then walks through, so the second match
    may not cost a retyped query.
    """
    async with create_session() as (pymux, state):
        popup = a_popup(pymux, state)
        type_a_search(state, MANY)

        press(state, "n")

        assert popup.buffer.cursor_position == matches_in(popup)[1]


@in_a_loop
async def test_the_match_before_is_one_key_away_as_well():
    async with create_session() as (pymux, state):
        popup = a_popup(pymux, state)
        type_a_search(state, MANY)
        press(state, "n")
        press(state, "n")

        press(state, "N")

        assert popup.buffer.cursor_position == matches_in(popup)[1]


@in_a_loop
async def test_a_count_moves_that_many_matches():
    """
    Three matches on, in one go.

    **The count is emacs's, because the popup is.** `mode-keys` decides
    the editing mode and it is emacs by default, as tmux's is, so a
    count is `M-3` and not a bare `3`. A bare digit is not a count
    here and moves one match, which is emacs's own behaviour and not a
    fault of the popup.
    """
    async with create_session() as (pymux, state):
        popup = a_popup(pymux, state)
        type_a_search(state, MANY)

        press(state, Keys.Escape, "3")
        press(state, "n")

        assert popup.buffer.cursor_position == matches_in(popup)[3]


@in_a_loop
async def test_repeating_a_search_needs_no_search_field():
    "The search field is closed while a person walks the matches."
    async with create_session() as (pymux, state):
        a_popup(pymux, state)
        type_a_search(state, MANY)

        press(state, "n")

        assert not searching(state)


# ----------------------------------------------------------------------
# Copy mode, which the issue says has the same gap.


@in_a_loop
async def test_copy_mode_opens_a_search_on_the_same_key():
    async with create_session() as (pymux, state):
        in_copy_mode(pymux, state)

        press(state, "/")

        assert searching(state)


@in_a_loop
async def test_copy_mode_holds_its_scrollback_read_only():
    """
    Which is the whole reason those keys reach it.

    `/`, `n` and `N` are bound against `is_read_only`
    (`prompt-toolkit/src/prompt_toolkit/key_binding/bindings/emacs.py`),
    so a read-only buffer gets all three and copy mode gets them for
    the same reason the popup does. Lillecarl/pymux#214.

    **Walking the matches in copy mode is not measured here.** It needs
    a pane that wrote text worth searching, and the pane in this
    harness writes none. That test belongs with copy mode, in
    `ptterm`, where a pane's content is what the harness controls.
    """
    async with create_session() as (pymux, state):
        copy_buffer = in_copy_mode(pymux, state)

        assert copy_buffer.read_only()
