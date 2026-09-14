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


def test_the_mode_over_a_pane_is_named_the_way_tmux_names_it(pymux):
    """
    `#{pane_mode}` is the name of what is drawn over the program, and
    tmux's names are `copy-mode` and `clock-mode`. Lillecarl/pymux#363.
    """
    pane = _context(pymux).pane

    assert format_pymux_string(pymux, "#{pane_mode}") == ""

    pane.clock_mode = True
    assert format_pymux_string(pymux, "#{pane_mode}") == "clock-mode"


def test_a_pane_showing_the_clock_is_in_a_mode(pymux):
    """
    `#{pane_in_mode}` read copy mode alone, so a pane drawing the clock
    answered "0" while `#{pane_mode}` named one. tmux counts the whole
    stack of modes (`format_cb_pane_in_mode`). Lillecarl/pymux#363.
    """
    pane = _context(pymux).pane

    assert format_pymux_string(pymux, "#{pane_in_mode}") == "0"

    pane.clock_mode = True
    assert format_pymux_string(pymux, "#{pane_in_mode}") == "1"


def test_the_prefix_a_client_holds_is_a_format(pymux):
    """
    `#{client_prefix}` says the client waits for the key after the
    prefix, and `#{client_key_table}` names the table that key is read
    from. tmux spells the two tables `root` and `prefix`.
    Lillecarl/pymux#363.
    """

    class Client:
        session = pymux.current_session
        has_prefix = False

    client = Client()

    assert format_pymux_string(pymux, "#{client_prefix}", client=client) == "0"
    assert format_pymux_string(pymux, "#{client_key_table}", client=client) == "root"

    client.has_prefix = True

    assert format_pymux_string(pymux, "#{client_prefix}", client=client) == "1"
    assert format_pymux_string(pymux, "#{client_key_table}", client=client) == "prefix"


def test_a_prefix_with_no_client_is_empty(pymux):
    "Not '0'. tmux answers nothing when it has no client to read."
    assert format_pymux_string(pymux, "#{client_prefix}") == ""
    assert format_pymux_string(pymux, "#{client_key_table}") == ""


def test_id_reads_as_target(pymux):
    "A caller passes these straight back as `-t`."
    assert format_pymux_string(pymux, "#{pane_id}").startswith("%")
    assert format_pymux_string(pymux, "#{window_id}").startswith("@")
    assert format_pymux_string(pymux, "#{session_id}") == "$0"
