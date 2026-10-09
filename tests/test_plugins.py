"""
A module in `pymux/plugins/` is a command like any other.
Lillecarl/pymux#529.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture

from pymux.commands import CommandParser, add_command, add_commands_to, handle_command
from pymux.plugins.count_panes import count_panes


async def test_the_example_plugin_runs_as_a_command(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")

        with set_app(pymux.connections[-1].client_state.app):
            for command in ("split-window", "new-window"):
                if (rest := handle_command(pymux, command)) is not None:
                    await rest
            pymux.command_output = []
            pymux.handle_command("count-panes")

        counts = [line.rpartition(" ")[2] for line in "\n".join(pymux.command_output).splitlines()]
        assert counts == ["2", "1"], pymux.command_output


async def test_list_commands_names_the_plugin():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("list-commands")

        assert any(line.startswith("count-panes ") for line in "\n".join(pymux.command_output).splitlines())


def test_a_plugin_cannot_take_the_name_of_a_command():
    subparsers = CommandParser(add_help=False).add_subparsers(parser_class=CommandParser)
    add_commands_to(subparsers)

    with pytest.raises(ValueError, match="kill-server"):
        add_command(subparsers, count_panes, name="kill-server")
