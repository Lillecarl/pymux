"""
Where "new-window" puts the window it opens.

A new window took the lowest free index from `base-index` up, whatever
window a person was looking at. On a session numbered one, two, five,
standing on five, `ctrl+b c` gave window three: a window at the far
end of the map a person had built, which they then had to go and find.

With no gaps the two rules agree, which is why this went so long
without being noticed. The lowest free index is the one after the last
window.

**The tests hold a client.** Which window is active is a fact about a
client, so `Arrangement.get_active_window` reads `get_app()`, and a
test with no application asks about a client that is not there and is
answered with the first window.

Lillecarl/pymux#191.
"""

from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app

from session import A_SIZE, NOTHING, in_a_loop, in_this_process


@asynccontextmanager
async def a_session(*indexes):
    """
    A server whose windows carry these indexes, active on the last.

    The windows are made and then numbered, because making one at an
    index is the thing under test and a fixture may not use it. It
    counts rather than creates one of its own first: the real server
    gives its first client a window at startup, and a fixture that
    made one too would have one more than it asked for.
    """
    with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("the client", A_SIZE)
        with set_app(state.app):
            while len(pymux.arrangement.windows) < len(indexes):
                pymux.create_window(NOTHING)
            assert len(pymux.arrangement.windows) == len(indexes)

            for window, index in zip(pymux.arrangement.windows, indexes):
                window.index = index
            pymux.arrangement.windows.sort(key=lambda w: w.index)
            pymux.arrangement.set_active_window(pymux.arrangement.windows[-1])
            yield pymux, state


def indexes(pymux):
    return [w.index for w in pymux.arrangement.windows]


def run(pymux, command):
    "Run a command the way a key binding or the command line does."
    pymux.handle_command("%s %s" % (command, NOTHING))


@in_a_loop
async def test_a_new_window_lands_next_to_the_one_a_person_is_on():
    "The report: on five of one, two, five, it gave three."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 5, 6]


@in_a_loop
async def test_it_makes_room_when_the_next_index_is_taken():
    async with a_session(1, 2, 3) as (pymux, _):
        pymux.arrangement.set_active_window(pymux.arrangement.windows[0])

        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 3, 4]
        assert pymux.arrangement.get_active_window().index == 2


@in_a_loop
async def test_a_session_with_no_gaps_is_what_it_always_was():
    "Which is why nobody saw this for so long."
    async with a_session(1, 2, 3) as (pymux, _):
        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 3, 4]


@in_a_loop
async def test_only_the_run_that_is_in_the_way_moves():
    "A gap stops the walk, so a window put out of the way stays there."
    async with a_session(1, 2, 3, 7) as (pymux, _):
        pymux.arrangement.set_active_window(pymux.arrangement.windows[0])

        run(pymux, "new-window")

        assert indexes(pymux) == [1, 2, 3, 4, 7]


@in_a_loop
async def test_before_the_active_window():
    "It takes that window's index, and that window moves up."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -b")

        assert indexes(pymux) == [1, 2, 5, 6]
        assert pymux.arrangement.get_active_window().index == 5


@in_a_loop
async def test_after_a_window_that_is_not_the_active_one():
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -a -t 1")

        assert indexes(pymux) == [1, 2, 3, 5]
        assert pymux.arrangement.get_active_window().index == 2


@in_a_loop
async def test_a_bare_target_names_the_index_to_open_at():
    "Which is how tmux reads one for this command."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -t 9")

        assert indexes(pymux) == [1, 2, 5, 9]


@in_a_loop
async def test_a_target_nobody_can_find_falls_back_to_the_active_window():
    """
    tmux errors there. A person who mistypes a window number while
    opening one does not want the window not to open.
    """
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -a -t nosuchwindow")

        assert indexes(pymux) == [1, 2, 5, 6]


@in_a_loop
async def test_the_window_that_is_reported_is_the_one_that_opened():
    """
    It read the last of the list, which was the new one only while a
    new window always took the highest index.
    """
    async with a_session(1, 2, 3) as (pymux, _):
        pymux.arrangement.set_active_window(pymux.arrangement.windows[0])

        run(pymux, "new-window")

        assert pymux.arrangement.get_active_window().index == 2


@in_a_loop
async def test_dash_d_leaves_the_window_a_person_was_on():
    "And the new one still lands where it would have."
    async with a_session(1, 2, 5) as (pymux, _):
        run(pymux, "new-window -d")

        assert indexes(pymux) == [1, 2, 5, 6]
        assert pymux.arrangement.get_active_window().index == 5
