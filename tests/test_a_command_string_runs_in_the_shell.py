"""
What a pane runs, by tmux's rule: a command given as one string goes
to `default-shell -c`, and a command given as several words runs as
they stand. Lillecarl/pyterm#935.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from run_commands import run_as
from session import create_session, once


async def test_one_string_is_shell_syntax(tmp_path):
    "`a; b` runs both, and a redirection writes the file."
    written = tmp_path / "out"
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "new-window 'echo one > %s; echo two >> %s; sleep 30'" % (written, written))
            await once(
                lambda: written.exists() and written.read_text() == "one\ntwo\n",
                10.0,
                lambda: "the file holds %r" % (written.read_text() if written.exists() else None),
            )


async def test_several_words_run_as_they_stand(tmp_path):
    "No shell reads them: the `;` reaches the program as an argument."
    written = tmp_path / "out"
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "new-window -- sh -c 'printf %%s \"$1\" > %s; sleep 30' sh 'a; b'" % written)
            await once(
                lambda: written.exists() and written.read_text() == "a; b",
                10.0,
                lambda: "the file holds %r" % (written.read_text() if written.exists() else None),
            )
