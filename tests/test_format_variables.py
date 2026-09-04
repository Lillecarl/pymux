"""
The format variables that a driver reads.

`format_pymux_string` answers an exception with an empty string. So a
variable that raises looks exactly like one that has nothing to say,
and a broken variable can sit there for as long as nobody looks.
`pane_synchronized` did: it took two arguments where every other takes
three.
"""
import sys

import pytest

from pymux.format import format_pymux_string, tmux_variables
from pymux.main import Pymux


@pytest.fixture
def pymux():
    "A server with one window, whose program ends at once."
    mux = Pymux()
    mux.create_window("%s -c pass" % (sys.executable,))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


@pytest.mark.parametrize("name", sorted(tmux_variables))
def test_no_variable_raises(pymux, name):
    "Call the handler itself, not the formatter that swallows for it."
    window = pymux.arrangement.get_active_window()
    tmux_variables[name](pymux, window, window.active_pane)


def test_a_variable_that_nobody_knows_is_empty(pymux):
    assert format_pymux_string(pymux, "#{not_a_variable}") == ""


def test_several_variables_in_one_string(pymux):
    answer = format_pymux_string(pymux, "#{session_name}:#{window_index}")
    assert answer.split(":")[0] == pymux.session_name


def test_an_id_reads_as_a_target(pymux):
    "A caller passes these straight back as `-t`."
    assert format_pymux_string(pymux, "#{pane_id}").startswith("%")
    assert format_pymux_string(pymux, "#{window_id}").startswith("@")
    assert format_pymux_string(pymux, "#{session_id}") == "$0"
