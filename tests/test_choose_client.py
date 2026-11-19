"""
`choose-client`, on prefix D as in tmux: the attached clients in a
box, and Enter or `d` detaches the pointed one. Lillecarl/pyterm#1050.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import create_session, in_this_process
from test_key_tables import listed

from pymux.enums import Chooser

SIZE = Size(rows=24, columns=80)


def run_at(state, text):
    "A command typed at this client, the way its command line runs one."
    with set_app(state.app):
        state.pymux.handle_command(text)


async def test_the_chooser_lists_the_attached_clients():
    async with in_this_process() as session:
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        run_at(here, "choose-client")

        assert here.chooser is Chooser.CLIENT
        assert here.layout_manager.chooser.chooser_matches() == [here, there]


async def test_choosing_a_client_detaches_it_and_keeps_this_one():
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        run_at(here, "choose-client")
        chooser = here.layout_manager.chooser
        here.choose_window_index = 1
        with set_app(here.app):
            assert chooser.pointed() is there
            chooser.choose_pointed()

        assert pymux.clients == [here]
        assert here.chooser is None


async def test_the_search_narrows_by_what_a_row_says():
    async with in_this_process() as session:
        here, _ = await session.attach("here", SIZE)
        await session.attach("there", SIZE)

        run_at(here, "choose-client")
        here.choose_window_filter.text = "nothing-is-called-this"

        assert here.layout_manager.chooser.chooser_matches() == []


async def test_prefix_d_opens_the_chooser():
    async with create_session() as (pymux, _state):
        rows = (await listed(pymux, "list-keys -T prefix")).splitlines()
        assert "bind-key -T prefix           D choose-client" in rows
