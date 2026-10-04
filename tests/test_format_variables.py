"""
The format variables that a driver reads.

`format_pymux_string` answers an exception with an empty string. So a
variable that raises looks exactly like one that has nothing to say,
and a broken variable can sit there for as long as nobody looks.
`pane_synchronized` did: it took two arguments where every other takes
four.
"""

from __future__ import annotations

import sys

import pytest

from pymux.format import (
    FormatContext,
    format_pymux_string,
    symbol_variables,
    tmux_variables,
)
from pymux.main import ClientState, Pymux


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

    assert format_pymux_string(pymux, "#{client_hostname}", client=Client()) == "buildbox-3"


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
    from. tmux spells the two tables `root` and `prefix`, and a mode
    puts its own name there for as long as it lasts.
    Lillecarl/pymux#363. Lillecarl/pymux#394.
    """

    class Client:
        session = pymux.current_session
        has_prefix = False
        key_tables = []
        active_key_table = ClientState.active_key_table

    client = Client()

    assert format_pymux_string(pymux, "#{client_prefix}", client=client) == "0"
    assert format_pymux_string(pymux, "#{client_key_table}", client=client) == "root"

    client.has_prefix = True

    assert format_pymux_string(pymux, "#{client_prefix}", client=client) == "1"
    assert format_pymux_string(pymux, "#{client_key_table}", client=client) == "prefix"

    client.has_prefix = False
    client.key_tables = ["pane-management"]

    assert format_pymux_string(pymux, "#{client_key_table}", client=client) == ("pane-management")


def test_a_prefix_with_no_client_is_empty(pymux):
    "Not '0'. tmux answers nothing when it has no client to read."
    assert format_pymux_string(pymux, "#{client_prefix}") == ""
    assert format_pymux_string(pymux, "#{client_key_table}") == ""


def test_id_reads_as_target(pymux):
    "A caller passes these straight back as `-t`."
    assert format_pymux_string(pymux, "#{pane_id}").startswith("%")
    assert format_pymux_string(pymux, "#{window_id}").startswith("@")
    assert format_pymux_string(pymux, "#{session_id}") == "$0"


def test_a_conditional_reads_a_value_the_way_tmux_reads_it(pymux):
    """
    `#{?cond,then,else}` takes the then for a value that is neither
    empty nor "0", and the else otherwise. Lillecarl/pymux#469.
    """
    assert format_pymux_string(pymux, "#{?pane_active,yes,no}") == "yes"
    assert format_pymux_string(pymux, "#{?pane_in_mode,yes,no}") == "no"
    assert format_pymux_string(pymux, "#{?not_a_variable,yes,no}") == "no"


def test_a_conditional_with_no_else_answers_nothing(pymux):
    """
    tmux's `list-clients` wraps the flags only when a client has some,
    and the else is what leaves a plain client's line alone.
    Lillecarl/pymux#469.
    """
    assert format_pymux_string(pymux, "#{?not_a_variable,(}") == ""
    assert format_pymux_string(pymux, "#{?pane_active,(}") == "("


def test_a_comparison_is_a_condition(pymux):
    "`#{==:a,b}` and `#{!=:a,b}` answer 1 or 0. Lillecarl/pymux#469."
    assert format_pymux_string(pymux, "#{==:a,a}") == "1"
    assert format_pymux_string(pymux, "#{==:a,b}") == "0"
    assert format_pymux_string(pymux, "#{!=:a,b}") == "1"


def test_a_branch_may_hold_a_comma_and_another_variable(pymux):
    """
    The comma inside a comparison belongs to it and not to the
    conditional around it, and a branch formats a variable of its own.
    Lillecarl/pymux#469.
    """
    template = "#{?#{==:#{pane_active},1},it is #{pane_index},no}"

    assert format_pymux_string(pymux, template) == "it is 0"


def test_a_bare_condition_is_a_variable_name_and_not_a_literal(pymux):
    """
    tmux looks the condition up as a name, so `#{?1,...}` names a
    variable called "1" and is false, while `#{?#{==:1,1},...}` is the
    comparison that is true. Lillecarl/pymux#469.
    """
    assert format_pymux_string(pymux, "#{?1,yes,no}") == "no"
    assert format_pymux_string(pymux, "#{?#{==:1,1},yes,no}") == "yes"


def test_a_comparison_reads_literals_and_needs_a_hash_for_a_variable(pymux):
    """
    A comparison operand is text unless it is written `#{...}`, so a
    bare name compares as itself. Lillecarl/pymux#469.
    """
    assert format_pymux_string(pymux, "#{==:session_name,#{session_name}}") == "0"
    assert format_pymux_string(pymux, "#{==:#{session_name},#{session_name}}") == "1"


def test_the_flags_of_a_client_are_bracketed_only_when_it_has_some(pymux):
    """
    tmux's `LIST_CLIENTS_TEMPLATE` ends with this shape, so a client
    with flags reads the flags and a plain one reads nothing.
    Lillecarl/pymux#469.
    """
    template = "#{?client_flags,(,}#{client_flags}#{?client_flags,),}"

    assert format_pymux_string(pymux, template) == ""

    class Client:
        session = pymux.current_session
        read_only = True
        ignore_size = False

    assert format_pymux_string(pymux, template, client=Client()) == "(read-only)"
