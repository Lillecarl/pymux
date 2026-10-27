"""
The prefix pressed twice sends the prefix to the pane, whatever key the
prefix is. tmux binds `C-b send-prefix` in its table, so moving the
prefix to C-a lost the way to send it until a person bound C-a too.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys
from run_commands import run
from session import create_session, once
from test_command_mode import press
from test_key_tables import listed, written_keys


def set_prefix(pymux, state, key):
    with set_app(state.app):
        pymux.handle_command("set-option prefix %s" % (key,))


async def test_the_default_prefix_twice_reaches_the_pane():
    async with create_session() as (pymux, state):
        written = written_keys(pymux)

        press(state, Keys.ControlB, Keys.ControlB)

        assert not state.has_prefix
        assert "".join(written) in ("\x02", "c-b")


async def test_a_moved_prefix_twice_reaches_the_pane_product_divergence():
    async with create_session() as (pymux, state):
        set_prefix(pymux, state, "C-a")
        written = written_keys(pymux)

        press(state, Keys.ControlA, Keys.ControlA)

        assert not state.has_prefix
        assert "".join(written) in ("\x01", "c-a")


async def test_the_old_prefix_is_a_plain_key_again():
    async with create_session() as (pymux, state):
        set_prefix(pymux, state, "C-a")
        written = written_keys(pymux)

        press(state, Keys.ControlB)

        assert not state.has_prefix
        assert "".join(written) in ("\x02", "c-b")


async def test_a_binding_of_ones_own_on_the_prefix_wins():
    async with create_session() as (pymux, state):
        before = len(pymux.arrangement.windows)
        await run(pymux, "bind-key C-b new-window")
        written = written_keys(pymux)

        press(state, Keys.ControlB, Keys.ControlB)

        await once(lambda: len(pymux.arrangement.windows) == before + 1, 5.0, "the binding never ran")
        assert "".join(written) == ""


# ----------------------------------------------------------------------
# It is built in, and listed as the binding it acts as.


async def test_which_key_lists_it_on_the_prefix():
    async with create_session() as (pymux, state):
        set_prefix(pymux, state, "C-a")

        rows = pymux.key_bindings_manager.prefix_keys()

        assert ("C-a", "send-prefix") in rows
        assert ("C-b", "send-prefix") not in rows


async def test_list_keys_lists_it_on_the_prefix():
    async with create_session() as (pymux, state):
        out = await listed(pymux, "list-keys -T prefix")

        assert "bind-key -T prefix           C-b send-prefix" in out


async def test_a_binding_of_ones_own_takes_its_row():
    async with create_session() as (pymux, state):
        await run(pymux, "bind-key C-b new-window")

        rows = pymux.key_bindings_manager.prefix_keys()

        assert ("C-b", "send-prefix") not in rows
        assert ("C-b", "new-window") in rows
