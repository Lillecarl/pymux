"""
The menu of `display-menu`.

A menu is a box of lines with a key each; the key of a line runs the
command it carries, Escape leaves, and a line answers a click. The
tests drive `menu_key_pressed` the way a key press would.
Lillecarl/pymux#297.
"""

from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_a_menu_opens_with_its_entries_and_title():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command(
                "display-menu -T ' The question ' 'Do this' o 'display did-this' 'Leave' q 'display left'"
            )

            assert state.menu_title == " The question "
            assert state.menu_entries == [
                ("o", "Do this", "display did-this"),
                ("q", "Leave", "display left"),
            ]

            rows = state.layout_manager._menu_tokens()
            assert "Do this" in "".join(text for _style, text, *_ in rows)


@in_a_loop
async def test_the_key_of_an_entry_runs_it():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display-menu 'Do this' o 'display did-this'")

            state.layout_manager.menu_key_pressed("o", "o")

        assert state.message == "did-this"
        assert not state.menu_entries


@in_a_loop
async def test_a_nobody_s_key_stays_in_the_menu():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display-menu 'Do this' o 'display did-this'")

            state.layout_manager.menu_key_pressed("z", "z")

        assert state.menu_entries


@in_a_loop
async def test_a_partial_triple_is_refused():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display-menu 'Do this' o")

        assert "menu entry" in state.message


@in_a_loop
async def test_clicking_a_row_runs_it():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display-menu 'Do this' o 'display did-this'")

            rows = state.layout_manager._menu_tokens()
            handler = rows[0][2]
            from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

            handler(
                MouseEvent(
                    position=None,
                    event_type=MouseEventType.MOUSE_DOWN,
                    button=1,
                    modifiers=frozenset(),
                )
            )

        assert state.message == "did-this"
        assert not state.menu_entries
