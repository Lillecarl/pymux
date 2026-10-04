"""
The shell state a pane reports: where it is, what it published, what
it marked and how its commands finished.

A shell reports its directory ("OSC 7", "CurrentDir"), its
variables ("SetUserVar"), its marks ("SetMark") and its command zones
("OSC 133") on every prompt. pymux keeps each per pane and forwards
the sequences on, so the outer terminal reads them for itself. An
attention ask ("RequestAttention") records a notification too, and a
clipboard write ("Copy") follows the clipboard option.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.main import MAX_MARKS, MAX_USER_VARS, Pymux
from pymux.notifications import Urgency
from pymux.options import Clipboard
from pymux.osc import (
    copy_data_of,
    current_dir_of,
    extension_key_of,
    file_url_dir_of,
    ftcs_of,
    request_attention_of,
    set_user_var_of,
)


def sequence(code, param):
    return "\x1b]%s;%s\x1b\\" % (code, param)


class FakeConnection:
    "A client connection that collects what pymux writes to it."

    def __init__(self):
        self.written = []

    def forward_osc(self, data):
        self.written.append(data)


class FakeScreen:
    "The screen parts a shell-state read touches."

    def __init__(self, cursor_y=3):
        self.cursor_y = cursor_y
        self.cleared = 0

    def clear_history(self):
        self.cleared += 1


class FakePane:
    """
    A pane with the shell-state fields of the real one.

    Keep in sync with `arrangement.Pane`: the directory, the host,
    the user variables, the marks, the zone and the exit status.
    """

    def __init__(self, pane_id=7, cursor_y=3):
        self.pane_id = pane_id
        self.screen = FakeScreen(cursor_y)
        self.current_directory = None
        self.current_host = None
        self.user_vars = {}
        self.marks = []
        self.command_zone = None
        self.last_exit_status = None


def make_pymux(clipboard=Clipboard.ON):
    pymux = Pymux()
    pymux.clipboard_mode = clipboard
    connections = [FakeConnection(), FakeConnection()]
    pymux._client_states = {connection: object() for connection in connections}
    return pymux, connections


# ----------------------------------------------------------------------
# Parsing the subcommands.


@pytest.mark.parametrize(
    ("param", "wanted"),
    [
        ("SetMark", ("SetMark", "")),
        ("CurrentDir=/home/you", ("CurrentDir", "/home/you")),
        ("SetUserVar=GIT=YmFy", ("SetUserVar", "GIT=YmFy")),
        ("Copy=:aGVsbG8=", ("Copy", ":aGVsbG8=")),
        ("ReportCellSize", ("ReportCellSize", "")),
    ],
)
def test_extension_key_splits_key_from_rest(param, wanted):
    assert extension_key_of(param) == wanted


@pytest.mark.parametrize(
    "param",
    [
        "",
        "=x",
        "1337;x",
        "Set Mark",
    ],
)
def test_extension_key_rejects_what_is_not_a_key(param):
    assert extension_key_of(param) is None


def test_extension_key_splits_file_payload_too():
    "A well-formed key the session does not serve still splits."
    assert extension_key_of("File=name=t.png;inline=1:AAAA") == (
        "File",
        "name=t.png;inline=1:AAAA",
    )


def test_set_user_var_decodes_base64():
    assert set_user_var_of("GIT=YmFy") == ("GIT", "bar")


@pytest.mark.parametrize(
    "rest",
    [
        "GIT",
        "=YmFy",
        "GI T=YmFy",
        "GIT=!",
        "GIT=",
    ],
)
def test_set_user_var_rejects_a_bad_name_or_value(rest):
    assert set_user_var_of(rest) is None


def test_current_dir_takes_a_plain_path():
    assert current_dir_of("/home/you") == "/home/you"


@pytest.mark.parametrize("rest", ["", "a\x07b"])
def test_current_dir_rejects_empty_and_controls(rest):
    assert current_dir_of(rest) is None


def test_file_url_gives_host_and_path():
    assert file_url_dir_of("file://host/home/you") == ("host", "/home/you")


def test_file_url_without_host_names_here():
    assert file_url_dir_of("file:///home/you") == (None, "/home/you")


def test_file_url_decodes_before_checking():
    assert file_url_dir_of("file://host/a%20b") == ("host", "/a b")
    assert file_url_dir_of("file://host/a%00b") is None


@pytest.mark.parametrize(
    "param",
    [
        "file://host",
        "host/home",
        "",
    ],
)
def test_file_url_rejects_what_is_not_a_url(param):
    assert file_url_dir_of(param) is None


@pytest.mark.parametrize("value", ["yes", "once", "no", "fireworks"])
def test_request_attention_takes_the_four_answers(value):
    assert request_attention_of("RequestAttention=" + value) == value


@pytest.mark.parametrize(
    "param",
    [
        "RequestAttention",
        "RequestAttention=maybe",
        "RequestAttention=yes;extra",
    ],
)
def test_request_attention_rejects_the_rest(param):
    assert request_attention_of(param) is None


def test_copy_takes_base64():
    assert copy_data_of("Copy=:aGVsbG8=") == "aGVsbG8="


@pytest.mark.parametrize(
    "param",
    [
        "Copy=",
        "Copy=:",
        "Copy=:not base64!",
        "CopyToClipboard=",
    ],
)
def test_copy_rejects_empty_and_not_base64(param):
    assert copy_data_of(param) is None


@pytest.mark.parametrize(
    ("param", "wanted"),
    [
        ("A", ("A", None)),
        ("B", ("B", None)),
        ("C", ("C", None)),
        ("D", ("D", None)),
        ("D;0", ("D", 0)),
        ("D;255", ("D", 255)),
    ],
)
def test_ftcs_names_zone_and_status(param, wanted):
    assert ftcs_of(param) == wanted


@pytest.mark.parametrize(
    "param",
    [
        "",
        "E",
        "AB",
        "A;x",
        "D;x",
        "D;256",
        "D;-1",
    ],
)
def test_ftcs_rejects_the_rest(param):
    assert ftcs_of(param) is None


# ----------------------------------------------------------------------
# Where the program is.


def test_osc7_stores_host_and_directory_and_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "7", "file://host/home/you")
    assert (pane.current_host, pane.current_directory) == ("host", "/home/you")
    for connection in connections:
        assert connection.written == [sequence("7", "file://host/home/you")]


def test_osc7_without_url_stores_nothing_but_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "7", "not a url")
    assert (pane.current_host, pane.current_directory) == (None, None)
    for connection in connections:
        assert connection.written == [sequence("7", "not a url")]


def test_current_dir_updates_directory_and_keeps_host():
    pymux, connections = make_pymux()
    pane = FakePane()
    pane.current_host = "host"
    pymux.forward_osc(pane, "1337", "CurrentDir=/home/me")
    assert (pane.current_host, pane.current_directory) == ("host", "/home/me")
    for connection in connections:
        assert connection.written == [sequence("1337", "CurrentDir=/home/me")]


def test_current_dir_with_nothing_in_it_is_dropped():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "CurrentDir=")
    assert pane.current_directory is None
    for connection in connections:
        assert connection.written == []


# ----------------------------------------------------------------------
# What the shell published.


def test_set_user_var_stores_decoded_and_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "SetUserVar=BRANCH=TWFpbg==")
    assert pane.user_vars == {"BRANCH": "Main"}
    for connection in connections:
        assert connection.written == [sequence("1337", "SetUserVar=BRANCH=TWFpbg==")]


def test_set_user_var_with_bad_value_is_dropped():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "SetUserVar=BRANCH=!")
    assert pane.user_vars == {}
    for connection in connections:
        assert connection.written == []


def test_user_vars_forget_oldest_first():
    pymux, _connections = make_pymux()
    pymux._client_states = {}
    pane = FakePane()
    for number in range(MAX_USER_VARS + 1):
        pymux.forward_osc(pane, "1337", "SetUserVar=K%d=eA==" % number)
    assert len(pane.user_vars) == MAX_USER_VARS
    assert "K0" not in pane.user_vars
    assert pane.user_vars["K%d" % MAX_USER_VARS] == "x"


# ----------------------------------------------------------------------
# What the program marked and how commands finished.


def test_set_mark_keeps_cursor_row_and_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane(cursor_y=5)
    pymux.forward_osc(pane, "1337", "SetMark")
    assert pane.marks == [5]
    for connection in connections:
        assert connection.written == [sequence("1337", "SetMark")]


def test_set_mark_with_arguments_is_dropped():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "SetMark=x")
    assert pane.marks == []
    for connection in connections:
        assert connection.written == []


def test_marks_forget_oldest_first():
    pymux, _connections = make_pymux()
    pymux._client_states = {}
    pane = FakePane()
    for _ in range(MAX_MARKS + 1):
        pymux.forward_osc(pane, "1337", "SetMark")
    assert pane.marks == [3] * MAX_MARKS


def test_zones_track_prompt_command_output():
    pymux, connections = make_pymux()
    pane = FakePane()
    for letter in "ABC":
        pymux.forward_osc(pane, "133", letter)
        assert pane.command_zone == letter
    for connection in connections:
        assert connection.written == [sequence("133", letter) for letter in "ABC"]


def test_zone_end_records_exit_status():
    pymux, _connections = make_pymux()
    pane = FakePane()
    pane.command_zone = "C"
    pymux.forward_osc(pane, "133", "D;3")
    assert pane.command_zone is None
    assert pane.last_exit_status == 3


def test_zone_abort_clears_zone_and_status():
    pymux, _connections = make_pymux()
    pane = FakePane()
    pane.command_zone = "B"
    pane.last_exit_status = 3
    pymux.forward_osc(pane, "133", "D")
    assert pane.command_zone is None
    assert pane.last_exit_status is None


def test_zone_garbage_stores_nothing_but_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "133", "E")
    assert (pane.command_zone, pane.last_exit_status) == (None, None)
    for connection in connections:
        assert connection.written == [sequence("133", "E")]


# ----------------------------------------------------------------------
# Asking for attention, copying, clearing.


@pytest.mark.parametrize(
    ("value", "urgency"),
    [
        ("yes", Urgency.CRITICAL),
        ("once", Urgency.NORMAL),
        ("fireworks", Urgency.NORMAL),
    ],
)
def test_attention_ask_records_hub_and_travels_on(value, urgency):
    pymux, connections = make_pymux()
    pane = FakePane(pane_id=7)
    pymux.forward_osc(pane, "1337", "RequestAttention=" + value)
    (record,) = pymux.notification_center.notifications()
    assert record.urgency == urgency
    assert record.pane_id == 7
    for connection in connections:
        assert connection.written == [sequence("1337", "RequestAttention=" + value)]


def test_attention_withdrawal_records_nothing_but_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "RequestAttention=no")
    assert pymux.notification_center.notifications() == []
    for connection in connections:
        assert connection.written == [sequence("1337", "RequestAttention=no")]


def test_attention_with_bad_value_is_dropped():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "RequestAttention=maybe")
    assert pymux.notification_center.notifications() == []
    for connection in connections:
        assert connection.written == []


def test_copy_reaches_paste_buffer_and_clients():
    pymux, connections = make_pymux()
    pymux.forward_osc(FakePane(), "1337", "Copy=:aGVsbG8=")
    assert pymux.clipboard.get_data().text == "hello"
    for connection in connections:
        assert connection.written == [sequence("1337", "Copy=:aGVsbG8=")]


@pytest.mark.parametrize("mode", [Clipboard.OFF, Clipboard.EXTERNAL])
def test_copy_follows_clipboard_option(mode):
    pymux, connections = make_pymux(clipboard=mode)
    pymux.forward_osc(FakePane(), "1337", "Copy=:aGVsbG8=")
    assert pymux.clipboard.get_data().text == ""
    for connection in connections:
        assert connection.written == []


def test_clear_scrollback_clears_screen_and_travels_on():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "ClearScrollback")
    assert pane.screen.cleared == 1
    for connection in connections:
        assert connection.written == [sequence("1337", "ClearScrollback")]


def test_clear_scrollback_with_arguments_is_dropped():
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", "ClearScrollback=x")
    assert pane.screen.cleared == 0
    for connection in connections:
        assert connection.written == []


# ----------------------------------------------------------------------
# What still stays inside.


@pytest.mark.parametrize(
    "param",
    [
        "File=name=t.png;inline=1:AAAA",
        "SetProfile=other",
        "RequestAttention",
        "SetMark=x",
        "CopyToClipboard=",
    ],
)
def test_unparsed_namespace_stays_inside(param):
    "Images wait for their slice; the rest is unchecked."
    pymux, connections = make_pymux()
    pane = FakePane()
    pymux.forward_osc(pane, "1337", param)
    assert pane.user_vars == {}
    assert pane.marks == []
    assert pane.current_directory is None
    assert pymux.notification_center.notifications() == []
    for connection in connections:
        assert connection.written == []


# ----------------------------------------------------------------------
# Reading the variables back.


async def test_list_user_vars_reads_active_pane():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            window = pymux.current_session.arrangement.get_active_window()
            pane = window.active_pane
            pane.user_vars["BRANCH"] = "main"
            pane.user_vars["APP"] = "pymux"

            pymux.handle_command("list-user-vars")

        assert state.message == "APP=pymux\nBRANCH=main"


async def test_list_user_vars_empty_pane_answers_empty():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("list-user-vars")

        assert state.message == ""


async def test_list_user_vars_missing_pane_is_an_error():
    async with create_session() as (pymux, state):
        errors = []
        pymux.add_command_error = errors.append
        with set_app(state.app):
            pymux.handle_command("list-user-vars -t %999999")

        assert errors == ["pymux: can't find pane: %999999"]
