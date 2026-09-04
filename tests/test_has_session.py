"""
Who `has-session` says yes to.

It is the command a driver asks first, so a wrong answer stops
everything that follows. With no target it means "is there a session",
and a server that answers at all has one.
"""
from pymux.commands.commands import _pane_matches_session_name
from pymux.main import Pymux


def test_no_target_asks_whether_there_is_a_session():
    "It compared the name against the empty string, so it always said no."
    assert _pane_matches_session_name(Pymux(), "")


def test_the_name_of_the_session_matches():
    pymux = Pymux()
    assert _pane_matches_session_name(pymux, pymux.session_name)


def test_the_exact_form_that_libtmux_sends_matches():
    pymux = Pymux()
    assert _pane_matches_session_name(pymux, "=" + pymux.session_name)


def test_another_name_does_not_match():
    assert not _pane_matches_session_name(Pymux(), "not-the-session")
