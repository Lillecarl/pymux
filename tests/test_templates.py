"""
The template language a person writes. Lillecarl/pymux#333.

A string holding `{{` or `{%` is a jinja2 template and everything else
is a tmux format, so a `.tmux.conf` carried over keeps working and a
person who wants a condition does not have to find an option first.

**`-F` is not sniffed.** It is a protocol: libtmux parses what it
answers and libpymux builds its templates out of `#{...}`. A listing
renders a template only when it is asked to.

A template sees the same facts under the same names: `{{ session_name }}`
is `#{session_name}`.
"""

import sys

import pytest

from pymux.format import (
    FormatContext,
    Language,
    format_pymux_string,
    holds_a_template,
    tmux_variables,
)
from pymux.jinja import BROKEN
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


@pytest.mark.parametrize(
    "string,is_template",
    [
        ("{{ session_name }}", True),
        ("{% if window_active %}x{% endif %}", True),
        ("#{session_name}", False),
        ("[#S] %H:%M", False),
        ("", False),
        ("{ not a template }", False),
    ],
)
def test_what_counts_as_a_template(string, is_template):
    assert holds_a_template(string) is is_template


def test_a_template_reads_the_same_facts(pymux):
    "`{{ session_name }}` is `#{session_name}`, and both are the name."
    assert format_pymux_string(pymux, "{{ session_name }}") == pymux.session_name
    assert format_pymux_string(pymux, "#{session_name}") == pymux.session_name


def test_a_template_can_ask_a_question(pymux):
    "The whole reason for this: the tmux language here cannot."
    assert (
        format_pymux_string(
            pymux, "{% if window_panes == '1' %}alone{% else %}shared{% endif %}"
        )
        == "alone"
    )


def test_a_template_can_do_arithmetic_and_filters(pymux):
    assert format_pymux_string(pymux, "{{ session_name | upper }}") == (
        pymux.session_name.upper()
    )
    assert format_pymux_string(pymux, "{{ (history_limit | int) + 1 }}") == str(
        pymux.history_limit + 1
    )


def test_a_name_nobody_knows_draws_nothing(pymux):
    "The same answer `#{not_a_variable}` gives."
    assert format_pymux_string(pymux, "{{ not_a_variable }}") == ""
    assert format_pymux_string(pymux, "{{ not_a_variable.nor_this }}") == ""


def test_a_template_that_does_not_parse_says_so(pymux):
    """
    Not silence. The tmux path loses one variable when it cannot
    answer it, and a template that does not parse loses the whole
    line, so the two must not look alike.
    """
    assert format_pymux_string(pymux, "{{ oh no }}") == BROKEN


def test_the_clock_a_template_prints_is_the_pinned_one(pymux):
    """
    `now` is `displayed_now()`, so a template holds still in a test the
    way a `%H:%M` status line does. Whole-string strftime is what this
    replaces: any `%` in a status line is a directive there.
    """
    pymux.test_mode = True

    drawn = format_pymux_string(pymux, "{{ now.strftime('%H:%M') }}")

    assert drawn == pymux.displayed_now().strftime("%H:%M")


def test_a_format_is_not_sniffed_when_the_language_is_named(pymux):
    "`-F` says tmux, so two braces are two braces."
    assert (
        format_pymux_string(pymux, "{{ session_name }}", language=Language.TMUX)
        == "{{ session_name }}"
    )


def test_a_tmux_format_can_be_asked_for_as_a_template(pymux):
    "`-J` says jinja, whatever the string looks like."
    assert format_pymux_string(pymux, "plain", language=Language.JINJA) == "plain"


def test_a_template_reads_the_client(pymux):
    "The context carries it, so `client_hostname` works here too."

    class Connection:
        hostname = "buildbox-3"

    class Client:
        connection = Connection()
        session = pymux.current_session

    drawn = format_pymux_string(
        pymux,
        "{% if client_hostname %}on {{ client_hostname }}{% endif %}",
        client=Client(),
    )

    assert drawn == "on buildbox-3"


def test_a_template_cannot_reach_past_the_facts(pymux):
    """
    The sandbox. A program in a pane can send `display-message`, and it
    can already send `run-shell`, so this closes nothing that is open --
    it is here so that a later lock on the command socket does not have
    to find this first.
    """
    assert format_pymux_string(pymux, "{{ session_name.__class__ }}") == ""
    assert format_pymux_string(pymux, "{{ ''.__class__.__mro__ }}") == ""


def test_only_the_facts_a_template_asks_for_are_read(pymux, monkeypatch):
    """
    A status line is formatted several times a frame, and there are
    forty variables. A template that asks for one costs one.
    """
    read = []

    for name in ("session_name", "pane_current_path", "history_size"):
        handler = tmux_variables[name]
        monkeypatch.setitem(
            tmux_variables,
            name,
            lambda context, name=name, handler=handler: (
                read.append(name) or handler(context)
            ),
        )

    format_pymux_string(pymux, "{{ session_name }}")

    assert read == ["session_name"]


def test_a_template_is_compiled_once(pymux):
    "The cache is what keeps this off the frame budget."
    from pymux.jinja import _compile

    first = _compile("{{ session_name }}")
    second = _compile("{{ session_name }}")

    assert first is second


def test_a_context_of_its_own_still_draws(pymux):
    "`format_in_context` is the seam a renderer with its own facts uses."
    from pymux.format import format_in_context

    session = pymux.current_session
    window = session.arrangement.get_active_window()
    context = FormatContext(pymux, session, window, window.active_pane, None)

    assert format_in_context(context, "{{ session_name }}") == session.name
