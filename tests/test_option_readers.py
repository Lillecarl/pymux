"""
`show-options`, `show-window-options` and `show-client-options`: the
read side of the option commands, with the wording of `set-option`
without a value.

The rules this judges, Lillecarl/pyterm#300: a name says what that
one holds, without one the scope lists itself one `name value` line
per option sorted, the three commands keep their scopes apart
(`Option.scope` says which), and `-g` on a window option reads what
every new window starts with.
"""

from __future__ import annotations

import argparse

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException
from pymux.commands.show_client_options import show_client_options
from pymux.commands.show_options import show_options
from pymux.commands.show_window_options import show_window_options
from pymux.options.options import (
    ALL_CLIENT_OPTIONS,
    ALL_OPTIONS,
    ALL_SERVER_OPTIONS,
    ALL_WINDOW_OPTIONS,
    KeyPrefixOption,
    Scope,
)


async def test_session_option_reads_as_it_is_written():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option status off")
            pymux.handle_command("show-options status")

        assert state.message == "off"


async def test_mode_keys_reads_the_word_tmux_reads():
    """
    The mode is a bool inside and a word outside. tmux answers `vi`
    and `emacs`; `on` answers nothing, and the completion offers the
    words the read-back would not say. Lillecarl/pyterm#384.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options mode-keys")

            assert state.message == "emacs"

            pymux.handle_command("set-option mode-keys vi")
            pymux.handle_command("show-options mode-keys")

            assert state.message == "vi"


async def test_session_list_holds_session_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "status" in names
        assert "strip" not in names  # a window option
        assert names == {name for name, o in ALL_OPTIONS.items() if o.scope is Scope.SESSION}


async def test_window_option_reads_active_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g strip on")
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "on"


async def test_base_index_reads_as_it_is_written():
    "It is set and working, so it must not read as not set. Lillecarl/pyterm#496."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option base-index 0")
            pymux.handle_command("show-options base-index")

        assert state.message == "0"


async def test_window_size_default_reads_as_it_is_written():
    "Lillecarl/pyterm#496."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g window-size latest")
            pymux.handle_command("show-window-options -g window-size")

        assert state.message == "latest"


async def test_window_size_reads_active_window():
    "Lillecarl/pyterm#496."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option window-size largest")
            pymux.handle_command("show-window-options window-size")

        assert state.message == "largest"


def test_every_option_names_where_it_lives():
    """
    An option that keeps its value in an attribute names it, or the
    read side answers "not set" for a value that is set. The prefix
    key is the one deliberate exception: it lives in the binding
    manager. Lillecarl/pyterm#496.
    """
    tables = (ALL_SERVER_OPTIONS, ALL_OPTIONS, ALL_WINDOW_OPTIONS, ALL_CLIENT_OPTIONS)
    for table in tables:
        for name, option in table.items():
            assert option.attribute_name is not None or isinstance(option, KeyPrefixOption), name


async def test_window_list_holds_window_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-window-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "strip" in names
        assert "status" not in names  # a session option


async def test_default_nobody_set_reads_as_not_set():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "not set"


async def test_client_option_reads_as_it_is_written():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-client-option theme grey")
            pymux.handle_command("show-client-options theme")

        assert state.message == "grey"


async def test_client_list_holds_client_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-client-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert names == set(ALL_CLIENT_OPTIONS)
        assert "status" not in names  # a session option
        assert "strip" not in names  # a window option


async def test_each_table_holds_the_options_of_its_scope():
    "A scope nobody lists is an option nobody can read back."
    wanted = {
        Scope.SERVER: ALL_SERVER_OPTIONS,
        Scope.SESSION: ALL_OPTIONS,
        Scope.WINDOW: ALL_WINDOW_OPTIONS,
        Scope.CLIENT: ALL_CLIENT_OPTIONS,
    }

    for scope, table in wanted.items():
        for name, option in table.items():
            assert option.scope is scope, name


def test_no_name_is_in_two_tables():
    "`set-option` finds a server option by its name, so a name has one table."
    tables = (ALL_SERVER_OPTIONS, ALL_OPTIONS, ALL_WINDOW_OPTIONS, ALL_CLIENT_OPTIONS)
    names = [name for table in tables for name in table]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("flag", ["", "-s "])
async def test_a_server_option_is_set_and_read_with_or_without_s(flag):
    "tmux's `options_scope_from_name`: the name finds the table. Lillecarl/pyterm#759."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option %sset-clipboard on" % flag)
            pymux.handle_command("show-options %sset-clipboard" % flag)

        assert state.message == "on"


async def test_the_server_list_holds_server_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options -s")

        names = {row.split()[0] for row in state.message.splitlines()}
        assert names == set(ALL_SERVER_OPTIONS)


async def test_the_session_list_leaves_the_server_out():
    "What tmux lists for `show-options`."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options")

        names = {row.split()[0] for row in state.message.splitlines()}
        assert "log-level" not in names
        assert "status" in names


async def test_a_known_option_is_set_in_its_own_scope_whichever_command_names_it():
    "tmux's `options_scope_from_name`: `setw -g mode-keys vi` holds. Lillecarl/pyterm#1030."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g mode-keys vi")
            assert pymux.session_defaults.mode_keys == "vi"
            pymux.handle_command("set-option -wg mode-keys emacs")
            assert pymux.session_defaults.mode_keys == "emacs"
            pymux.handle_command("set-option -g monitor-activity on")
            assert pymux.window_defaults["monitor_activity"] is True


async def test_a_appends_to_a_string_option_and_sets_any_other():
    "tmux's `set -a`, `options_set_string`. Lillecarl/pyterm#1031."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option -g status-right one")
            pymux.handle_command("set-option -ag status-right ' two'")
            assert pymux.session_defaults.status_right == "one two"

            pymux.handle_command("set-window-option -g pane-title-format a")
            pymux.handle_command("set-window-option -ga pane-title-format b")
            assert pymux.window_defaults["pane_title_format"] == "ab"

            pymux.handle_command("set-option -ag base-index 3")
            assert pymux.session_defaults.base_index == 3


async def test_a_user_option_is_kept_and_read_back():
    "tmux's `@name`: a string pymux gives no meaning. Lillecarl/pyterm#1032."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option -g @plugin tmux-plugins/tpm")
            pymux.handle_command("set-option -ag @plugin /sensible")
            pymux.handle_command("show-options -gv @plugin")
            assert state.message == "tmux-plugins/tpm/sensible"

            pymux.handle_command("set-window-option -g @theme dark")
            pymux.handle_command("set-option -s @where server")
            assert pymux.user_options == {
                Scope.SERVER: {"@where": "server"},
                Scope.WINDOW: {"@theme": "dark"},
                Scope.SESSION: {"@plugin": "tmux-plugins/tpm/sensible"},
            }

            pymux.handle_command("set-option -gu @plugin")
            pymux.handle_command("show-options -gqv @plugin")
            assert state.message == ""
            with pytest.raises(CommandException, match="Invalid option: @plugin"):
                show_options(pymux, argparse.Namespace(g=1, option="@plugin", target_session=None))


async def test_a_user_option_has_one_value_for_every_session_window_and_pane():
    "A value of one's own is Lillecarl/pyterm#1038, and the refusal says so."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option @mine x")
            assert "Lillecarl/pyterm#1038" in state.message
            pymux.handle_command("set-option -p @mine x")
            assert "every pane" in state.message
            assert pymux.user_options[Scope.SESSION] == {}


async def test_q_sets_quietly_past_an_option_pymux_does_not_have():
    "tmux's `set -q`, which configurations write for an option of another version."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option -gq no-such-option on")

        assert state.message is None


async def test_escape_time_is_how_long_a_bare_escape_waits():
    "tmux's `escape-time`, which every client's key processor reads. Lillecarl/pyterm#1035."
    async with create_session() as (pymux, state):
        assert state.app.ttimeoutlen == 0.5
        with set_app(state.app):
            pymux.handle_command("set-option -sg escape-time 10")

        assert state.app.ttimeoutlen == 0.01


async def test_focus_events_off_counts_every_terminal_as_focused():
    "tmux's `focus-events`; pymux's default is on. Lillecarl/pyterm#1035."
    async with create_session() as (pymux, state):
        assert pymux.options.focus_events is True
        state.terminal_focused = False
        with set_app(state.app):
            pymux.handle_command("set-option -s focus-events off")
            told = set(pymux.focus._focused_panes)
            pymux.handle_command("split-window")
            pymux.focus.sync_panes()

        assert state.terminal_focused is True
        # tmux tells no pane its focus while the option is off.
        assert pymux.focus._focused_panes == told


async def test_a_window_command_names_set_client_option_for_a_client_option():
    "tmux has no client options, so the command that takes one is still named."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option theme grey")

        assert state.message == "theme is a client option: use set-client-option"


async def test_wrong_kind_of_option_is_unknown():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_options(pymux, argparse.Namespace(g=False, option="strip"))

        with pytest.raises(CommandException):
            show_window_options(pymux, argparse.Namespace(g=False, option="status"))

        with pytest.raises(CommandException):
            show_client_options(pymux, argparse.Namespace(target_client=None, option="status"))

        with pytest.raises(CommandException):
            show_options(pymux, argparse.Namespace(g=False, option="theme"))


async def test_unknown_option_is_unknown():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_options(pymux, argparse.Namespace(g=False, option="not-an-option"))


async def test_list_commands_lists_every_command_with_its_description():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("list-commands")

        rows = state.message.splitlines()
        names = [row.split()[0] for row in rows]
        assert names == sorted(names)
        assert "list-commands" in names
        assert "swap-window" in names
        # Each row carries the first line of the handler's docstring.
        row = next(one for one in rows if one.split()[0] == "swap-window")
        assert "Swap" in row


async def test_refresh_client_asks_its_own_app_for_frame():
    async with create_session() as (pymux, state):
        asked = []
        real = state.app.invalidate
        state.app.invalidate = lambda: (asked.append(True), real())[1]

        with set_app(state.app):
            pymux.handle_command("refresh-client")

        assert asked
