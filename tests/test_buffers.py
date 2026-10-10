"""
The named buffers: `set-buffer`, `show-buffer -b`, `list-buffers`,
`delete-buffer`, `load-buffer` and `save-buffer`.

The rules this judges, Lillecarl/pymux#303: a `-b` name reaches a
store of its own on the server; without one the session's one buffer
is the buffer, and `paste-buffer` pastes it. A name nothing holds is
an error on every read and write of it.
"""

from __future__ import annotations

import argparse

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.layout.containers import ConditionalContainer, DynamicContainer
from session import create_session

from pymux.commands import CommandException
from pymux.commands.delete_buffer import delete_buffer
from pymux.commands.save_buffer import save_buffer
from pymux.commands.show_buffer import show_buffer
from pymux.enums import Chooser


async def test_set_buffer_without_name_fills_session_buffer():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer hello")

        assert pymux.clipboard.get_data().text == "hello"


async def test_named_buffer_round_trips():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b mine hello")

        assert pymux.named_buffers["mine"] == "hello"

        with set_app(state.app):
            pymux.handle_command("show-buffer -b mine")
        assert state.layout_manager.popup_dialog.title == "show-buffer"


async def test_list_says_how_much_each_buffer_holds():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b a hello")
            pymux.handle_command("set-buffer -b b hey")
            pymux.handle_command("list-buffers")

        assert state.message.splitlines() == ["a 5", "b 3"]


async def test_delete_buffer_removes_name():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b mine hello")
            pymux.handle_command("delete-buffer -b mine")

        assert "mine" not in pymux.named_buffers


async def test_buffer_nobody_holds_is_error():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            delete_buffer(pymux, argparse.Namespace(buffer_name="nope"))

        with pytest.raises(CommandException):
            show_buffer(pymux, argparse.Namespace(buffer_name="nope"))

        with pytest.raises(CommandException):
            save_buffer(pymux, argparse.Namespace(b="nope", filename="/tmp/opencode/none"))


async def test_load_and_save_buffer_move_file(tmp_path):
    async with create_session() as (pymux, state):
        source = tmp_path / "in.txt"
        source.write_text("from a file\n")
        target = tmp_path / "out.txt"

        with set_app(state.app):
            pymux.handle_command("load-buffer -b mine %s" % (source,))

        assert pymux.named_buffers["mine"] == "from a file\n"

        with set_app(state.app):
            pymux.handle_command("save-buffer -b mine %s" % (target,))

        assert target.read_text() == "from a file\n"


async def test_load_buffer_without_name_fills_session_buffer(tmp_path):
    async with create_session() as (pymux, state):
        source = tmp_path / "in.txt"
        source.write_text("piped")

        with set_app(state.app):
            pymux.handle_command("load-buffer %s" % (source,))

        assert pymux.clipboard.get_data().text == "piped"


async def test_buffer_nobody_holds_is_error():
    async with create_session() as (pymux, state):
        with set_app(state.app), pytest.raises(CommandException):
            delete_buffer(pymux, argparse.Namespace(buffer_name="nope"))

        with set_app(state.app), pytest.raises(CommandException):
            show_buffer(pymux, argparse.Namespace(buffer_name="nope"))

        with set_app(state.app), pytest.raises(CommandException):
            save_buffer(pymux, argparse.Namespace(buffer_name="nope", filename="/tmp/opencode/none"))


async def test_buffer_chooser_opens_and_lists_buffers():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b a hello")
            pymux.handle_command("set-buffer -b b hey")
            pymux.handle_command("choose-buffer")

        assert state.chooser is Chooser.BUFFER
        rows = state.layout_manager._choose_buffer_tokens()
        assert len(rows) == 2
        assert "a" in rows[0][1] and "5" in rows[0][1]


async def test_the_buffer_chooser_rolls_over_at_the_ends():
    """
    The point wraps for every chooser, and this one walks the same
    `point_at`: one past the last buffer is the first, and one before
    the first is the last.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b a hello")
            pymux.handle_command("set-buffer -b b hey")
            pymux.handle_command("choose-buffer")

            last = len(state.layout_manager.chooser_matches()) - 1
            assert last == 1

            state.layout_manager.point_at(-1)
            assert state.choose_window_index == last

            state.layout_manager.point_at(last + 1)
            assert state.choose_window_index == 0


async def test_enter_from_buffer_chooser_fills_session_buffer():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b mine hello")
            pymux.handle_command("choose-buffer")

        state.layout_manager.choose_pointed_buffer()

        assert state.chooser is not Chooser.BUFFER
        assert pymux.clipboard.get_data().text == "hello"


async def test_search_narrows_buffer_rows():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-buffer -b mine hello")
            pymux.handle_command("set-buffer -b yours hey")
            pymux.handle_command("choose-buffer")

            state.choose_window_filter.insert_text("mi")

        rows = state.layout_manager._choose_buffer_tokens()
        assert len(rows) == 1
        assert "mine" in rows[0][1]
        assert state.choose_window_index == 0


async def test_choosers_are_one_at_time():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("choose-buffer")
            pymux.handle_command("choose-window")

        assert state.chooser is Chooser.WINDOW


def _box_float(state):
    "The container that shows a box chooser when its filter says so."
    for container in state.app.layout.walk():
        if (
            isinstance(container, ConditionalContainer)
            and isinstance(container.content, DynamicContainer)
            and container.content.get_container == state.layout_manager._chooser_box
        ):
            return container
    raise AssertionError("no float holds the box chooser")


@pytest.mark.parametrize("command", ["choose-buffer", "customize-mode", "choose-notifications", "choose-job"])
async def test_every_box_chooser_draws_and_keeps_its_search(command):
    """
    Every kind the box holds shows it, and keeps the focus on its search
    line through the hook that runs after each key.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command(command)
            assert _box_float(state).filter()

            search = state.layout_manager.chooser_search_control()
            state.app.layout.focus(search)
        state.sync_focus()

        with set_app(state.app):
            assert state.app.layout.has_focus(search)
