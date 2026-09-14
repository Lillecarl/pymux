"""
A question waiting for an answer does not take the status line.

`confirm-before` drew its question on the bottom row, over the status
line, while the message toolbar drew one row up and left it alone.
Same corner, same job -- say something and wait -- and two different
rules about the row underneath, which nobody had chosen: the two
surfaces grew separately and the first pictures of them were the first
time they had been seen together.

Carl picked the message toolbar's rule for both. The status line is
where a person reads which window they are on, and `kill-window #W?`
is the moment they want it: the question names something the window
list is the context for.

Lillecarl/pymux#339.
"""

from prompt_toolkit.data_structures import Size

from pymux.layout import Z_INDEX
from session import in_this_process

SIZE = Size(rows=24, columns=80)


def floats_of(state):
    return state.layout_manager.layout.floats


def the_confirmation_float(state):
    "The float the question draws in."
    for one in floats_of(state):
        if one.z_index == Z_INDEX.CONFIRMATION:
            return one
    raise AssertionError("the layout draws no confirmation anywhere")


def the_message_float(state):
    for one in floats_of(state):
        if one.z_index == Z_INDEX.MESSAGE_TOOLBAR:
            return one
    raise AssertionError("the layout draws no message toolbar")


async def test_a_question_sits_above_the_bottom_row():
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE)

        assert the_confirmation_float(state).bottom == 1


async def test_it_sits_where_a_message_sits():
    "The point of the change: one rule, not two."
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE)

        assert (
            the_confirmation_float(state).bottom
            == the_message_float(state).bottom
        )


async def test_a_question_draws_over_a_message():
    """
    The two can meet: a command that said something and then asked.
    The question wins, because it is the one waiting on the person.
    """
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE)

        assert Z_INDEX.CONFIRMATION > Z_INDEX.MESSAGE_TOOLBAR


async def test_the_command_line_still_takes_the_bottom_row():
    """
    Only the question moved. The `:` command line is tmux's shape and
    was not part of the decision, so it stays where it was.
    """
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE)

        bottoms = [one.bottom for one in floats_of(state)]
        assert 0 in bottoms, "nothing draws on the bottom row any more"
