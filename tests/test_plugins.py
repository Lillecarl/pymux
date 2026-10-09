"""
A module in `pymux/plugins/` is a command like any other.
Lillecarl/pymux#529.
"""

from __future__ import annotations

from importlib import import_module

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture

from pymux import commands, plugins
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


#: A plugin as a person writes one, in their own configuration.
GREET = '''
from pymux.commands import add_command
from pymux.commands.common import answer


def greet_from_home(pymux, args):
    """Say where this command came from."""
    answer(pymux, "hello from home")


def register(subparsers):
    add_command(subparsers, greet_from_home)
'''

#: Two plugins that must not take the others down with them.
BROKEN = "raise RuntimeError('this plugin is broken')\n"
CLASHING = GREET.replace(
    "add_command(subparsers, greet_from_home)", 'add_command(subparsers, greet_from_home, name="kill-server")'
)


def _home(tmp_path, **plugins):
    plugins_dir = tmp_path / "pymux" / "plugins"
    plugins_dir.mkdir(parents=True)
    for name, text in plugins.items():
        (plugins_dir / ("%s.py" % name)).write_text(text)
    return {"XDG_CONFIG_HOME": str(tmp_path)}


async def test_a_plugin_in_the_configuration_directory_runs(tmp_path, monkeypatch):
    environ = _home(tmp_path, greet_runs=GREET)
    monkeypatch.setenv("XDG_CONFIG_HOME", environ["XDG_CONFIG_HOME"])
    monkeypatch.setattr(commands, "_parser_tree", None)

    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("greet-from-home")

        assert pymux.command_output == ["hello from home"]


def test_a_broken_plugin_is_left_out_and_the_rest_load(tmp_path, caplog):
    environ = _home(tmp_path, a_broken=BROKEN, b_clashing=CLASHING, c_greet=GREET)
    subparsers = CommandParser(add_help=False).add_subparsers(parser_class=CommandParser)
    for name in commands.MODULES:
        import_module("pymux.commands." + name).register(subparsers)

    plugins.register(subparsers, environ)

    refused = {record.args[0] for record in caplog.records if "failed to load" in record.msg}
    assert refused == {"a_broken", "b_clashing"}
    assert subparsers.choices["kill-server"].get_default("_handler").__module__ == "pymux.commands.kill_server"
    assert "count-panes" in subparsers.choices
    assert "greet-from-home" in subparsers.choices


def test_a_plugin_cannot_take_the_name_of_a_command():
    subparsers = CommandParser(add_help=False).add_subparsers(parser_class=CommandParser)
    add_commands_to(subparsers)

    with pytest.raises(ValueError, match="kill-server"):
        add_command(subparsers, count_panes, name="kill-server")
