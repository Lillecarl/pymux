"""
The overlay pane: a pane that floats in the middle of the screen.

It belongs to the session, like a window does, so every client sees the
same one. It takes the keyboard while it is open, and it closes when
the program in it ends.
"""
import pytest

from pymux.layout import DEFAULT_OVERLAY_SIZE, MIN_OVERLAY_SIZE, overlay_size
from pymux.main import Pymux


# ----------------------------------------------------------------------
# How large it is.


@pytest.mark.parametrize(
    "given,available,expected",
    [
        (None, 80, 48),  # The default is 60%.
        ("50%", 80, 40),
        ("100%", 80, 80),
        ("20", 80, 20),
        ("200", 80, 80),  # Never wider than the screen.
        ("1", 80, MIN_OVERLAY_SIZE),  # Never too small to show anything.
        ("nonsense", 80, 48),  # Back to the default.
        ("", 80, 48),
        ("60%", 45, 27),
    ],
)
def test_the_size_of_an_overlay(given, available, expected):
    assert overlay_size(given, available) == expected


def test_the_default_is_a_share_of_the_screen():
    assert DEFAULT_OVERLAY_SIZE.endswith("%")


# ----------------------------------------------------------------------
# Opening and closing it.


def test_a_session_starts_without_an_overlay():
    pymux = Pymux()
    assert pymux.overlay_pane is None


def test_opening_an_overlay_starts_a_pane():
    pymux = Pymux()
    pane = pymux.display_overlay(command="%s -c pass" % _python(), title="a title")
    try:
        assert pymux.overlay_pane is pane
        assert pymux.overlay_title == "a title"
        # The overlay takes the keyboard.
        assert pymux.get_focused_pane() is pane
    finally:
        pymux.close_overlay()


def test_the_title_falls_back_to_the_command():
    pymux = Pymux()
    command = "%s -c pass" % _python()
    pymux.display_overlay(command=command)
    try:
        assert pymux.overlay_title == command
    finally:
        pymux.close_overlay()


def test_a_second_overlay_replaces_the_first():
    pymux = Pymux()
    first = pymux.display_overlay(command="%s -c pass" % _python())
    second = pymux.display_overlay(command="%s -c pass" % _python())
    try:
        assert first is not second
        assert pymux.overlay_pane is second
    finally:
        pymux.close_overlay()


def test_closing_an_overlay_gives_the_keyboard_back():
    pymux = Pymux()
    pymux.display_overlay(command="%s -c pass" % _python())
    pymux.close_overlay()
    assert pymux.overlay_pane is None


def test_closing_when_there_is_none_is_fine():
    Pymux().close_overlay()  # Does not raise.


def test_every_client_looks_at_the_overlay():
    "The overlay belongs to the session, so it has the focus for all."

    class FakeClientState:
        app = None

    class FakePane:
        pass

    pymux = Pymux()
    pane = pymux.display_overlay(command="%s -c pass" % _python())
    try:
        assert pymux._has_focus(FakeClientState(), pane) is True
        assert pymux._has_focus(FakeClientState(), FakePane()) is False
    finally:
        pymux.close_overlay()


def _python():
    import sys

    return sys.executable
