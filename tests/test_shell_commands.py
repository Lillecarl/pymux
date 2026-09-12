"""
`run-shell` and `if-shell`: the server runs a shell for a command.

run-shell shows what the command said; if-shell runs one of two
commands by its answer. Both block the server while the shell runs,
the way tmux's do, so the tests keep to `true` and `false`, which
answer in no time. Lillecarl/pymux#297.
"""

import asyncio

import pytest
from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_run_shell_shows_what_the_command_said():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("run-shell echo hello-from-the-shell")

        for _ in range(100):
            dialog = state.layout_manager.popup_dialog
            if dialog is not None and dialog.title == "run-shell":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the output never arrived")


@in_a_loop
async def test_run_shell_from_the_command_line_answers_on_it():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.command_output = []
            pymux.handle_command("run-shell echo straight-back")

        assert any("straight-back" in line for line in pymux.command_output)
        pymux.command_output = None


@in_a_loop
async def test_if_shell_runs_the_then_command_on_zero():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell true 'display yes' 'display no'")

        assert state.message == "yes"


@in_a_loop
async def test_if_shell_runs_the_else_command_otherwise():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell false 'display yes' 'display no'")

        assert state.message == "no"


@in_a_loop
async def test_if_shell_takes_a_format_for_a_question():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell -F 'i-am-a-format' 'display format-said-yes' 'display no'")

        assert state.message == "format-said-yes"
