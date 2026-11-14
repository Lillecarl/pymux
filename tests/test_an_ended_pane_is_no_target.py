"""
A pane that ended and that no session holds is no target: `-t %N`
fails as for an id that never was, at once, and not whenever the
collector drops the pane.

`panes_by_id` holds panes weakly. A killed pane anybody still referred
to went on answering `capture-pane` and `wait-pane-change` with its
last screen and exit 0, so a mirror of it froze with no error.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import in_this_process, once

from pymux.commands import CommandException, handle_command
from pymux.commands.common import find_pane, the_pane
from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)


async def _command(pymux, state, text):
    "Run a command as the person at this client, and wait for it."
    with set_app(state.app):
        answer = handle_command(pymux, text)
        if answer is not None:
            await answer


async def test_a_killed_pane_is_no_target_while_something_still_holds_it():
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        await _command(pymux, state, "split-window 'sleep 30'")
        pane = pymux.arrangement.get_active_pane()
        target = "%%%s" % (pane.pane_id,)

        await _command(pymux, state, "kill-pane -t %s" % (target,))
        await once(lambda: pane.process.is_terminated, 5.0, "the killed pane's program never ended")

        assert find_pane(pymux, target) is None
        with pytest.raises(CommandException, match="can't find pane: %s" % (target,)):
            the_pane(pymux, target)


async def test_a_pane_that_remains_on_exit_is_a_target_until_it_is_killed():
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        await _command(pymux, state, "set-option -g remain-on-exit on")
        await _command(pymux, state, "split-window 'true'")
        pane = pymux.arrangement.get_active_pane()
        target = "%%%s" % (pane.pane_id,)
        await once(lambda: pane.process.is_terminated, 5.0, "the program never ended")

        assert find_pane(pymux, target) is pane

        await _command(pymux, state, "kill-pane -t %s" % (target,))
        assert find_pane(pymux, target) is None
