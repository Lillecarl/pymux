"""
Key tables: the mechanism every pymux mode shares.

A mode was a hand-wired flag and a pile of one-off Conditions, and the
next mode would have repeated all of it. Now it is a named table a
client sits in: `bind-key -T <table>` fills it, `enter-mode` puts the
client in it, and while it is on top its bindings answer and the root
table holds nothing. The prefix works inside a mode, one key at a
time, the way it does in tmux's own modes. Lillecarl/pymux#394.

Pane-management is the first mode in the table. Lillecarl/pymux#395.
"""

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys

from pymux.commands import handle_command
from pymux.main import Pymux

from session import create_session
from test_command_mode import press


@pytest.fixture
def pymux():
    return Pymux()


def run(pymux, command):
    "Run one command and give back what it said about it."
    before = len(pymux.message_log)
    handle_command(pymux, command)
    return list(pymux.message_log)[before:]


def run_as_client(pymux, state, command):
    "Run one command the way the client that pressed the key runs it."
    with set_app(state.app):
        handle_command(pymux, command)


def in_mode(pymux, state, name="pane-management"):
    run_as_client(pymux, state, "enter-mode %s" % (name,))


def written_keys(pymux):
    "Swap the focused pane's input for a list that catches what lands."
    pane = pymux.arrangement.get_active_pane()
    written = []
    pane.process.write_input = written.append
    return written


def split(pymux):
    "A second pane, so focus has somewhere to go."
    run(pymux, "split-window -v")


# ----------------------------------------------------------------------
# The table of a binding.


def test_bind_into_a_named_table(pymux):
    "-T names the table, and the first -T names a mode nobody had."
    run(pymux, "bind-key -T my-mode x new-window")

    manager = pymux.key_bindings_manager
    assert "my-mode" in manager.mode_tables
    assert manager.binding_on("x", table="my-mode").command == "new-window"
    assert manager.binding_on("x") is None


def test_n_still_binds_the_root_table(pymux):
    "The spelling every configuration file already has keeps its meaning."
    run(pymux, "bind-key -n x new-window")

    assert pymux.key_bindings_manager.binding_on("x", table="root") is not None


def test_one_key_is_bindings_apart_by_table(pymux):
    "The same key in three tables is three bindings."
    run(pymux, "bind-key -n x new-window")
    run(pymux, "bind-key x kill-window")
    run(pymux, "bind-key -T my-mode x kill-pane")

    manager = pymux.key_bindings_manager
    assert manager.binding_on("x", table="root").command == "new-window"
    assert manager.binding_on("x").command == "kill-window"
    assert manager.binding_on("x", table="my-mode").command == "kill-pane"


def test_unbind_from_the_table_it_was_bound_into(pymux):
    "A binding under another name's table is not the one this touches."
    run(pymux, "bind-key -T my-mode x new-window")
    run(pymux, "unbind-key x")

    assert pymux.key_bindings_manager.binding_on("x", table="my-mode") is not None

    run(pymux, "unbind-key -T my-mode x")

    assert pymux.key_bindings_manager.binding_on("x", table="my-mode") is None


def test_prefix_keys_list_skips_the_other_tables(pymux):
    "which-key draws what the prefix reaches, and nothing else."
    run(pymux, "bind-key -T my-mode x new-window")
    run(pymux, "bind-key -n y new-window")

    rows = pymux.key_bindings_manager.prefix_keys()

    assert ("x", "new-window") not in rows
    assert ("y", "new-window") not in rows


# ----------------------------------------------------------------------
# Entering and leaving.


async def test_enter_mode_puts_its_table_on_top():
    async with create_session() as (pymux, state):
        in_mode(pymux, state)

        assert state.key_tables == ["pane-management"]
        assert state.active_key_table == "pane-management"


async def test_leave_mode_puts_root_back():
    async with create_session() as (pymux, state):
        in_mode(pymux, state)
        run_as_client(pymux, state, "leave-mode")

        assert state.key_tables == []
        assert state.active_key_table == "root"


async def test_entering_an_unknown_mode_is_said_so():
    async with create_session() as (pymux, state):
        complaints = run(pymux, "enter-mode no-such-mode")

        assert complaints
        assert "no-such-mode" in complaints[0]
        assert state.key_tables == []


async def test_leaving_with_no_mode_to_leave_is_said_so():
    async with create_session() as (pymux, state):
        complaints = run(pymux, "leave-mode")

        assert complaints


async def test_entering_a_mode_holds_the_pane_numbers_up():
    async with create_session() as (pymux, state):
        assert not pymux.display_pane_numbers

        in_mode(pymux, state)

        assert pymux.display_pane_numbers

        run_as_client(pymux, state, "leave-mode")

        assert not pymux.display_pane_numbers


async def test_a_prefix_pressed_inside_a_mode_sits_on_top_of_it():
    """
    The prefix works inside a mode, one key at a time: after the key
    is read, the mode is still there. The window the key made counts
    for it. Lillecarl/pymux#394, decision 2.
    """
    async with create_session() as (pymux, state):
        before = len(pymux.arrangement.windows)
        in_mode(pymux, state)

        press(state, Keys.ControlB, "c")

        assert len(pymux.arrangement.windows) == before + 1
        assert state.key_tables == ["pane-management"]
        assert not state.has_prefix


# ----------------------------------------------------------------------
# What a mode's keys do, and what the others do.


async def test_a_bare_key_reaches_the_pane_while_the_mode_holds():
    """
    A key the table does not name is the pane's. That is what makes
    the mode transparent, and it is the answer a mode keeps unless it
    binds `Any` itself.
    """
    async with create_session() as (pymux, state):
        written = written_keys(pymux)
        in_mode(pymux, state)

        press(state, "m")

        assert "m" in "".join(written)


async def test_the_mode_s_own_keys_answer_without_the_prefix():
    async with create_session() as (pymux, state):
        split(pymux)
        pane_before = pymux.arrangement.get_active_window().active_pane
        in_mode(pymux, state)

        press(state, "o")

        assert (
            pymux.arrangement.get_active_window().active_pane is not pane_before
        )


async def test_the_mode_masks_the_root_table():
    """
    A `-n` binding is somebody's always-on key, and a mode is the one
    thing that may take the key back for a while: the table on top is
    the only one that answers.
    """
    async with create_session() as (pymux, state):
        run(pymux, "bind-key -n c-e new-window")
        before = len(pymux.arrangement.windows)

        in_mode(pymux, state)
        press(state, Keys.ControlE)

        assert len(pymux.arrangement.windows) == before

        run_as_client(pymux, state, "leave-mode")
        press(state, Keys.ControlE)

        assert len(pymux.arrangement.windows) == before + 1


async def test_strictness_is_a_binding_of_any():
    """
    `Any` is the key that is every key, and `noop` is what swallows
    them. Bound together in a table, the mode is strict: nothing its
    own keys do not name reaches the pane.
    """
    async with create_session() as (pymux, state):
        run(pymux, "bind-key -T strict x new-window")
        run(pymux, "bind-key -T strict Any noop")
        written = written_keys(pymux)

        in_mode(pymux, state, "strict")
        press(state, "a")

        assert "".join(written) == ""


async def test_q_leaves_the_default_pane_management_mode():
    async with create_session() as (pymux, state):
        in_mode(pymux, state)

        press(state, "q")

        assert state.key_tables == []


async def test_escape_leaves_the_default_pane_management_mode():
    async with create_session() as (pymux, state):
        in_mode(pymux, state)

        press(state, Keys.Escape)

        assert state.key_tables == []


async def test_prefix_m_enters_it():
    "The key the initial configuration binds entry to."
    async with create_session() as (pymux, state):
        press(state, Keys.ControlB, "M")

        assert state.key_tables == ["pane-management"]


# ----------------------------------------------------------------------
# What the formats and the listing say.


async def test_client_key_table_names_the_mode():
    async with create_session() as (pymux, state):
        assert state.active_key_table == "root"

        in_mode(pymux, state)

        assert state.active_key_table == "pane-management"

        state.has_prefix = True

        assert state.active_key_table == "prefix"


def listed(pymux, command):
    "Run a listing the way the command line runs it, and read it back."
    pymux.command_output = []
    run(pymux, command)
    out, pymux.command_output = "\n".join(pymux.command_output), None
    return out


def test_list_keys_spells_the_table(pymux):
    run(pymux, "bind-key -T my-mode x new-window")
    run(pymux, "bind-key y kill-window")

    out = listed(pymux, "list-keys")

    assert "-T my-mode" in out
    assert "x new-window" in out
    assert "-T prefix" in out
    assert "y kill-window" in out


def test_list_keys_only_one_table(pymux):
    run(pymux, "bind-key -T my-mode x new-window")
    run(pymux, "bind-key y kill-window")

    out = listed(pymux, "list-keys -T my-mode")

    assert "my-mode" in out
    assert "y kill-window" not in out
