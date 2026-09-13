"""
The format variables that a driver reads.

`format_pymux_string` answers an exception with an empty string. So a
variable that raises looks exactly like one that has nothing to say,
and a broken variable can sit there for as long as nobody looks.
`pane_synchronized` did: it took two arguments where every other takes
four.
"""

import sys

import pytest

from pymux.format import (
    FormatContext,
    format_pymux_string,
    symbol_variables,
    tmux_variables,
)
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


def _context(pymux, client=None) -> FormatContext:
    session = pymux.current_session
    window = session.arrangement.get_active_window()
    return FormatContext(pymux, session, window, window.active_pane, client)


@pytest.mark.parametrize("name", sorted(tmux_variables))
def test_no_variable_raises(pymux, name):
    "Call the handler itself, not the formatter that swallows for it."
    tmux_variables[name](_context(pymux))


@pytest.mark.parametrize("symbol", sorted(symbol_variables))
def test_no_symbol_raises(pymux, symbol):
    "The `#X` spellings read the same context, and are asked the same."
    symbol_variables[symbol](_context(pymux))


def test_variable_that_nobody_knows_is_empty(pymux):
    assert format_pymux_string(pymux, "#{not_a_variable}") == ""


def test_several_variables_in_one_string(pymux):
    answer = format_pymux_string(pymux, "#{session_name}:#{window_index}")
    assert answer.split(":")[0] == pymux.session_name


def test_the_hostname_is_spelled_the_way_tmux_spells_it(pymux, monkeypatch):
    """
    `#H` is the whole name and `#h` is the name without its domain.
    That is round this way in tmux (`format.c`: "host" is H and
    "host_short" is h), and pymux had `#h` answering the whole name and
    no `#H` at all. Lillecarl/pymux#331.
    """
    monkeypatch.setattr("socket.gethostname", lambda: "dynhetz.example.net")

    assert format_pymux_string(pymux, "#H") == "dynhetz.example.net"
    assert format_pymux_string(pymux, "#h") == "dynhetz"
    assert format_pymux_string(pymux, "#{host}") == "dynhetz.example.net"
    assert format_pymux_string(pymux, "#{host_short}") == "dynhetz"


def test_a_hostname_with_no_domain_is_itself(pymux, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "dynhetz")

    assert format_pymux_string(pymux, "#h") == "dynhetz"
    assert format_pymux_string(pymux, "#H") == "dynhetz"


def test_the_client_is_what_a_client_variable_reads(pymux):
    """
    `#{client_hostname}` is the machine the client runs on, which only
    a client can say: `#{host}` here is the server's own, and over ssh
    those are two machines. Lillecarl/pymux#287, Lillecarl/pymux#330.
    """

    class Connection:
        hostname = "buildbox-3"

    class Client:
        connection = Connection()
        session = pymux.current_session

    assert (
        format_pymux_string(pymux, "#{client_hostname}", client=Client())
        == "buildbox-3"
    )


def test_a_client_variable_with_no_client_is_empty(pymux):
    "A command formats without one, and tmux answers nothing there too."
    assert format_pymux_string(pymux, "#{client_hostname}") == ""


def test_id_reads_as_target(pymux):
    "A caller passes these straight back as `-t`."
    assert format_pymux_string(pymux, "#{pane_id}").startswith("%")
    assert format_pymux_string(pymux, "#{window_id}").startswith("@")
    assert format_pymux_string(pymux, "#{session_id}") == "$0"
