"""
Full screen mode hides the decoration without forgetting it.

A person who turns full screen on wants one pane over every cell. A
person who turns it off again wants their status line back, the way
they set it. So the option overrides the other two; it does not write
over them.

`tests/drive_with_pty.py::check_a_full_screen_pane` measures the cells
themselves, over a real pty.
"""
from pymux.main import Pymux
from pymux.options import ALL_OPTIONS


def a_pymux():
    return Pymux()


def set_option(pymux, name, value):
    ALL_OPTIONS[name].set_value(pymux, value)


def test_a_new_server_draws_the_status_line_and_the_pane_titlebar():
    pymux = a_pymux()
    assert pymux.show_status
    assert pymux.show_pane_status


def test_full_screen_hides_both():
    pymux = a_pymux()
    set_option(pymux, "full-screen", "on")
    assert not pymux.show_status
    assert not pymux.show_pane_status


def test_full_screen_leaves_the_two_options_as_the_person_set_them():
    pymux = a_pymux()
    set_option(pymux, "full-screen", "on")
    assert pymux.enable_status
    assert pymux.enable_pane_status


def test_turning_full_screen_off_gives_the_decoration_back():
    pymux = a_pymux()
    set_option(pymux, "full-screen", "on")
    set_option(pymux, "full-screen", "off")
    assert pymux.show_status
    assert pymux.show_pane_status


def test_a_status_line_that_was_off_stays_off():
    pymux = a_pymux()
    set_option(pymux, "status", "off")
    set_option(pymux, "full-screen", "on")
    set_option(pymux, "full-screen", "off")
    assert not pymux.show_status
    assert pymux.show_pane_status


def test_the_status_line_costs_a_row_and_full_screen_gives_it_back():
    """
    The size that a pane gets counts the status line. Full screen has
    to reach that arithmetic too, or the pane is one row short of the
    screen it covers.
    """
    pymux = a_pymux()
    assert pymux.show_status
    set_option(pymux, "full-screen", "on")
    assert not pymux.show_status
