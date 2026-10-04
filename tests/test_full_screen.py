"""
Full screen hides the decoration without forgetting it.

A person who turns full screen on wants one pane over every cell. A
person who turns it off again wants their status line back, the way
they set it. So the option overrides the other two; it does not write
over them.

**It belongs to one terminal.** `set-client-option full-screen on`, and
not `set-option`, because a person watching on a phone drops the chrome
while the person working keeps it. So every question here is asked of
a client, under that client's own application, which is how a frame
asks it. Lillecarl/pymux#471.

The plane is the other half and it is shared: it keeps the rows the
chrome of any watching client needs, and a full-screen client draws
background in them. `test_two_clients_of_different_sizes.py` holds the
rest of what two clients on one window do.

`tests/drive_with_pty.py::check_full_screen_pane` measures the cells
themselves, over a real pty.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import in_this_process

from pymux.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS, SetOptionError

#: One terminal with room, and one without.
BIG = Size(rows=24, columns=100)
SMALL = Size(rows=12, columns=40)

#: A window. `plane_size` answers the session's default size while
#: nobody watches, so the sizing tests need a client and a window.
NOTHING = "%s -c pass" % ("python3",)


def set_option(pymux, state, name, value):
    """
    What `set-client-option` or `set-option` does, under that client.

    The table says which command it is: `full-screen` is in the client
    one and `status` is in the session one, which is the split this
    file is about.
    """
    table = ALL_CLIENT_OPTIONS if name in ALL_CLIENT_OPTIONS else ALL_OPTIONS
    with set_app(state.app):
        table[name].set_value(pymux, value)


def shows(pymux, state) -> tuple:
    "What that client draws for itself: the status line, the titlebars."
    with set_app(state.app):
        return (pymux.show_status, pymux.show_pane_status)


# ----------------------------------------------------------------------
# One client.


async def test_a_new_client_draws_the_status_line_and_the_titlebars():
    async with in_this_process() as session:
        state, _ = await session.attach("only", BIG)
        assert shows(session.pymux, state) == (True, True)


async def test_full_screen_hides_both():
    async with in_this_process() as session:
        state, _ = await session.attach("only", BIG)
        set_option(session.pymux, state, "full-screen", "on")
        assert shows(session.pymux, state) == (False, False)


async def test_full_screen_leaves_the_two_options_as_a_person_set_them():
    async with in_this_process() as session:
        state, _ = await session.attach("only", BIG)
        set_option(session.pymux, state, "full-screen", "on")
        assert session.pymux.enable_status
        assert session.pymux.enable_pane_status


async def test_turning_full_screen_off_gives_the_decoration_back():
    async with in_this_process() as session:
        state, _ = await session.attach("only", BIG)
        set_option(session.pymux, state, "full-screen", "on")
        set_option(session.pymux, state, "full-screen", "off")
        assert shows(session.pymux, state) == (True, True)


async def test_a_status_line_that_was_off_stays_off():
    async with in_this_process() as session:
        state, _ = await session.attach("only", BIG)
        set_option(session.pymux, state, "status", "off")
        set_option(session.pymux, state, "full-screen", "on")
        set_option(session.pymux, state, "full-screen", "off")
        assert shows(session.pymux, state) == (False, True)


async def test_the_status_line_costs_a_row_and_full_screen_gives_it_back():
    """
    The size a pane gets counts the status line. Full screen has to
    reach that arithmetic too, or the pane is one row short of the
    screen it covers.
    """
    async with in_this_process() as session:
        pymux = session.pymux
        pymux.create_window(NOTHING)
        state, _ = await session.attach("only", BIG)

        with_chrome = pymux.plane_size().rows
        set_option(pymux, state, "full-screen", "on")

        assert pymux.plane_size().rows == with_chrome + 1
        assert pymux.plane_size().rows == BIG.rows


# ----------------------------------------------------------------------
# Two clients, one of them watching.


async def _one_of_each(session):
    "A person working with chrome, and a person watching without it."
    session.pymux.create_window(NOTHING)
    working, _ = await session.attach("working", BIG)
    watching, _ = await session.attach("watching", BIG)
    set_option(session.pymux, watching, "full-screen", "on")
    return working, watching


async def test_each_client_draws_its_own_chrome():
    async with in_this_process() as session:
        working, watching = await _one_of_each(session)

        assert shows(session.pymux, working) == (True, True)
        assert shows(session.pymux, watching) == (False, False)


async def test_the_plane_keeps_the_row_the_other_client_needs():
    """
    **The plane is shared**, so the status row stays while anybody
    watching draws one, and the full-screen client draws background in
    it. The other way round would resize everybody's programs because
    somebody else turned an option off.
    """
    async with in_this_process() as session:
        working, watching = await _one_of_each(session)

        assert session.pymux.plane_size().rows == BIG.rows - 1


async def test_the_plane_gives_the_row_back_when_the_last_one_goes():
    async with in_this_process() as session:
        working, watching = await _one_of_each(session)
        set_option(session.pymux, working, "full-screen", "on")

        assert session.pymux.plane_size().rows == BIG.rows


async def test_a_full_screen_client_still_sizes_the_plane():
    "It is watching, so its columns count. Only its chrome does not."
    async with in_this_process() as session:
        session.pymux.create_window(NOTHING)
        await session.attach("working", BIG)
        watching, _ = await session.attach("watching", SMALL)
        set_option(session.pymux, watching, "full-screen", "on")

        assert session.pymux.plane_size().columns == SMALL.columns


# ----------------------------------------------------------------------
# What it is not.


async def test_set_option_says_which_command_takes_it():
    """
    `set-option` sees the session's options only, so the line a person
    had in their configuration file before this moved has to say what
    to write instead. A bare "invalid option" reads as a typo.
    """
    async with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", BIG)

        with set_app(state.app):
            pymux.handle_command("set-option full-screen on")

        assert state.message == ("full-screen is a client option: use set-client-option")
        assert shows(pymux, state) == (True, True)


def test_nobody_attached_cannot_set_it():
    "A client option belongs to a terminal, and there is none."
    from pymux.main import Pymux

    with pytest.raises(SetOptionError):
        ALL_CLIENT_OPTIONS["full-screen"].set_value(Pymux(), "on")
