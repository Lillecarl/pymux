"""
`bind-key -r` and `bind-key -N`, as tmux has them. Lillecarl/pyterm#1033.

After a key bound with `-r` runs from the prefix, the next such key
works without the prefix for `repeat-time` milliseconds. Any other key
ends that and goes where it would have gone.
"""

from __future__ import annotations

import time

from prompt_toolkit.keys import Keys
from session import create_session, once
from test_command_mode import press
from test_key_tables import listed, run_as_client, written_keys

from pymux.options.options import Scope


def hits(pymux) -> str:
    return pymux.user_options[Scope.SESSION].get("@hits", "")


async def until_hits(pymux, wanted: str) -> None:
    await once(lambda: hits(pymux) == wanted, 5.0, lambda: "@hits is %r, not %r" % (hits(pymux), wanted))


async def test_a_repeat_key_works_again_without_the_prefix():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "bind-key -r x set-option -ga @hits x")

        press(state, Keys.ControlB, "x", "x", "x")

        await until_hits(pymux, "xxx")


async def test_another_key_ends_the_repeat_and_reaches_the_pane():
    async with create_session() as (pymux, state):
        written = written_keys(pymux)
        run_as_client(pymux, state, "bind-key -r x set-option -ga @hits x")

        press(state, Keys.ControlB, "x", "q", "x")

        await until_hits(pymux, "x")
        assert "qx" in "".join(written)
        assert state.repeat_until == 0.0


async def test_a_key_without_r_needs_the_prefix_each_time():
    async with create_session() as (pymux, state):
        written = written_keys(pymux)
        run_as_client(pymux, state, "bind-key y set-option -ga @hits y")

        press(state, Keys.ControlB, "y", "y")

        await until_hits(pymux, "y")
        assert "y" in "".join(written)


async def test_the_repeat_ends_after_repeat_time():
    async with create_session() as (pymux, state):
        written = written_keys(pymux)
        run_as_client(pymux, state, "set-option -g repeat-time 0")
        run_as_client(pymux, state, "bind-key -r x set-option -ga @hits x")

        press(state, Keys.ControlB, "x", "x")
        await until_hits(pymux, "x")
        assert "x" in "".join(written)

        run_as_client(pymux, state, "set-option -g repeat-time 500")
        press(state, Keys.ControlB, "x")
        await until_hits(pymux, "xx")
        state.repeat_until = time.monotonic() - 1
        press(state, "x")
        assert "xx" in "".join(written)


async def test_list_keys_shows_r_and_n_shows_the_notes():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "bind-key -r -N 'Wider left' H resize-pane -L 5")
        run_as_client(pymux, state, "bind-key z resize-pane -Z")

        rows = (await listed(pymux, "list-keys -T prefix")).splitlines()
        assert "bind-key -r -T prefix           H resize-pane -L 5" in rows
        assert "bind-key    -T prefix           z resize-pane -Z" in rows
        assert "bind-key    -T prefix           C-b send-prefix" in rows

        assert await listed(pymux, "list-keys -N") == "C-b H Wider left"


async def test_bind_without_a_command_changes_the_note_and_the_repeat():
    "tmux's `key_bindings_add` with no command list."
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "bind-key z resize-pane -Z")
        run_as_client(pymux, state, "bind-key -r -N Zoom z")
        run_as_client(pymux, state, "bind-key -N 'No such key' F12")

        binding = pymux.key_bindings_manager.binding_on("z")
        assert (binding.command, binding.arguments, binding.repeat, binding.note) == (
            "resize-pane",
            ["-Z"],
            True,
            "Zoom",
        )
        assert pymux.key_bindings_manager.binding_on("F12") is None
