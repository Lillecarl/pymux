"""
`split-window -d` leaves the focus where it was, and `-P` names the
pane the split made, focused or not.
"""

from __future__ import annotations

import sys

from pymux.answer import Answer
from pymux.commands import call_command_handler
from pymux.main import Pymux

SLEEPS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


async def run(mux, command: str, *arguments) -> tuple[str, list[str]]:
    errors: list[str] = []
    mux.add_command_error = errors.append
    mux.show_message = lambda _message: None
    mux.answer = Answer()
    try:
        answer = call_command_handler(command, mux, list(arguments))
        if answer is not None:
            await answer
        return "\n".join(mux.answer.output), errors
    finally:
        mux.answer = None


async def split(*flags) -> tuple[str, object, object, object]:
    "What -P printed, the pane that was active before, the active pane after, and the new pane."
    mux = Pymux()
    try:
        async with mux.running():
            await run(mux, "new-session", "-d", "-s", "work", SLEEPS)
            (session,) = [one for one in mux.sessions if one.name == "work"]
            window = session.arrangement.windows[0]
            before = window.active_pane
            printed, errors = await run(
                mux, "split-window", *flags, "-P", "-F", "#{pane_id}", "-t", "@%d" % window.window_id, SLEEPS
            )
            assert not errors, errors
            (new,) = [pane for pane in window.panes if pane is not before]
            return printed, before, window.active_pane, new
    finally:
        for pane in list(mux.panes_by_id.values()):
            if not pane.process.is_terminated:
                pane.process.kill()


async def test_a_split_focuses_the_new_pane():
    printed, _before, after, new = await split()
    assert after is new
    assert printed == "%%%d" % new.pane_id


async def test_d_leaves_the_focus_where_it_was():
    printed, before, after, new = await split("-d")
    assert after is before
    assert printed == "%%%d" % new.pane_id
