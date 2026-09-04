"""
libpymux without a server.

These cover what the library does with what a server says: the format
strings it asks for, the rows it reads back, the commands it builds and
the quoting it puts around them. `drive_with_pty.py` runs the same
library against a server that is really there.
"""
import json
import socket
import threading

import pytest

from libpymux import CommandError, Pane, Server, Window, quote
from libpymux.connection import CommandResult, Connection
from libpymux.objects import _PANE_FIELDS, _format_string, _rows, _SEPARATOR


# ----------------------------------------------------------------------
# A server that says what a test tells it to say.


class FakeServer:
    """
    A unix socket that answers one command and closes, the way the
    real server does.
    """

    def __init__(self, tmp_path, answers):
        self.path = str(tmp_path / "fake.sock")
        self.answers = list(answers)
        self.asked = []
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(self.path)
        self.socket.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            with connection:
                data = b""
                while b"\0" not in data:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                packet = json.loads(data.split(b"\0", 1)[0].decode("utf-8"))
                self.asked.append(packet["data"])

                answer = self.answers.pop(0) if self.answers else ("", "", 0)
                out, err, code = answer
                for kind, text in (("out", out), ("err", err)):
                    if text:
                        connection.sendall(
                            json.dumps({"cmd": kind, "data": text}).encode() + b"\0"
                        )
                connection.sendall(
                    json.dumps({"cmd": "exit", "code": code}).encode() + b"\0"
                )

    def close(self):
        self.socket.close()


@pytest.fixture
def fake(tmp_path):
    servers = []

    def make(answers=()):
        server = FakeServer(tmp_path, answers)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def pane_row(**values):
    "One line of `list-panes -F`, with the fields this library asks for."
    defaults = {name: "" for name in _PANE_FIELDS}
    defaults.update(values)
    return _SEPARATOR.join(defaults[name] for name in _PANE_FIELDS)


# ----------------------------------------------------------------------
# The wire.


def test_a_command_reaches_the_server(fake):
    server = fake([("hello\n", "", 0)])
    result = Server(server.path).cmd("list-windows")
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    assert server.asked == ["list-windows"]


def test_a_list_of_arguments_is_quoted(fake):
    server = fake([("", "", 0)])
    Server(server.path).cmd(["send-keys", "-l", "two words"])
    assert server.asked == ["send-keys -l 'two words'"]


def test_a_failing_command_raises(fake):
    server = fake([("", "no such window\n", 1)])
    with pytest.raises(CommandError) as raised:
        Server(server.path).cmd("select-window -t 99")
    assert "no such window" in str(raised.value)
    assert raised.value.result.exit_code == 1


def test_a_failing_command_can_be_asked_not_to_raise(fake):
    server = fake([("", "nope\n", 1)])
    result = Server(server.path).cmd("has-session -t x", check=False)
    assert not result.ok
    assert result.stderr == "nope\n"


def test_has_session_reads_the_exit_code(fake):
    server = fake([("", "", 0), ("", "can't find session\n", 1)])
    server_object = Server(server.path)
    assert server_object.has_session("here")
    assert not server_object.has_session("gone")


def test_a_server_that_is_not_there(tmp_path):
    server = Server(str(tmp_path / "nothing.sock"))
    assert not server.is_alive()


# ----------------------------------------------------------------------
# The format strings, and the rows that come back.


def test_the_format_string_asks_for_every_field():
    text = _format_string(["pane_id", "pane_index"])
    assert text == "#{pane_id}" + _SEPARATOR + "#{pane_index}"


def test_the_separator_is_not_something_a_terminal_writes():
    # A tab or a space would come back inside a pane title.
    assert _SEPARATOR not in " \t|:,;"


def test_a_row_becomes_a_dictionary():
    rows = _rows(CommandResult("a\x1fb\n", "", 0), ["one", "two"])
    assert rows == [{"one": "a", "two": "b"}]


def test_a_short_row_fills_the_fields_it_misses():
    "An older server knows fewer variables, and answers with fewer."
    rows = _rows(CommandResult("a\n", "", 0), ["one", "two"])
    assert rows == [{"one": "a", "two": ""}]


def test_empty_output_is_no_rows():
    assert _rows(CommandResult("", "", 0), ["one"]) == []


# ----------------------------------------------------------------------
# The objects.


def test_the_panes_of_a_server(fake):
    rows = "\n".join(
        [
            pane_row(pane_id="%1", pane_index="0", window_id="@0", pane_active="1"),
            pane_row(pane_id="%2", pane_index="1", window_id="@0", pane_active="0"),
        ]
    )
    server = fake([(rows + "\n", "", 0)])
    panes = Server(server.path).panes
    assert [pane.id for pane in panes] == ["%1", "%2"]
    assert [pane.index for pane in panes] == [0, 1]
    assert [pane.active for pane in panes] == [True, False]
    assert server.asked[0].startswith("list-panes -a -F ")


def test_a_pane_reads_its_numbers(fake):
    row = pane_row(
        pane_id="%7",
        pane_width="80",
        pane_height="24",
        pane_pid="4242",
        pane_title="a title",
        pane_current_command="fish",
        pane_dead="0",
    )
    server = fake([(row + "\n", "", 0)])
    pane = Server(server.path).panes[0]
    assert (pane.width, pane.height) == (80, 24)
    assert pane.pid == 4242
    assert pane.title == "a title"
    assert pane.current_command == "fish"
    assert not pane.dead


def test_a_field_that_the_server_left_empty_is_not_a_crash(fake):
    server = fake([(pane_row(pane_id="%1") + "\n", "", 0)])
    pane = Server(server.path).panes[0]
    assert pane.width == 0
    assert pane.pid == -1
    assert pane.index == -1


def test_the_panes_of_a_window_are_the_ones_it_holds(fake):
    rows = "\n".join(
        [
            pane_row(pane_id="%1", window_id="@0"),
            pane_row(pane_id="%2", window_id="@1"),
            pane_row(pane_id="%3", window_id="@1"),
        ]
    )
    server = fake([(rows + "\n", "", 0)])
    window = Window(Server(server.path), {"window_id": "@1"})
    assert [pane.id for pane in window.panes] == ["%2", "%3"]


def test_send_keys_sends_the_text_and_then_the_return(fake):
    server = fake([("", "", 0), ("", "", 0)])
    Pane(Server(server.path), {"pane_id": "%5"}).send_keys("echo hi")
    assert server.asked == [
        "send-keys -t %5 -l 'echo hi'",
        "send-keys -t %5 Enter",
    ]


def test_send_keys_without_the_return(fake):
    server = fake([("", "", 0)])
    Pane(Server(server.path), {"pane_id": "%5"}).send_keys("hi", enter=False)
    assert server.asked == ["send-keys -t %5 -l hi"]


def test_send_keys_can_name_a_key(fake):
    server = fake([("", "", 0)])
    Pane(Server(server.path), {"pane_id": "%5"}).send_key("C-c")
    assert server.asked == ["send-keys -t %5 C-c"]


def test_capture_asks_for_the_lines_it_was_given(fake):
    server = fake([("one\ntwo\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%5"})
    assert pane.capture(start=-10, end=0) == "one\ntwo\n"
    assert server.asked == ["capture-pane -p -t %5 -S -10 -E 0"]


def test_capture_without_lines_asks_for_the_screen(fake):
    server = fake([("", "", 0)])
    Pane(Server(server.path), {"pane_id": "%5"}).capture()
    assert server.asked == ["capture-pane -p -t %5"]


def test_splitting_a_window_returns_the_new_pane(fake):
    server = fake([(pane_row(pane_id="%9", pane_index="1") + "\n", "", 0)])
    window = Window(Server(server.path), {"window_id": "@0", "window_index": "0"})
    pane = window.split(command="fish")
    assert pane.id == "%9"
    asked = server.asked[0]
    assert asked.startswith("split-window -t @0 -v ")
    assert asked.endswith(" fish")
    assert " -P -F " in asked


def test_splitting_the_other_way(fake):
    server = fake([("", "", 0)])
    window = Window(Server(server.path), {"window_id": "@0", "window_index": "0"})
    assert window.split(vertical=False) is None
    assert server.asked[0].startswith("split-window -t @0 -h ")


def test_a_new_window_carries_its_name(fake):
    # Reading the session asks first, and making the window asks second.
    server = fake(
        [
            ("$0\x1fwork\x1f1\x1f1\x1f/home\x1f0\n", "", 0),
            ("@4\x1f2\x1fbuild\x1f1\x1f*\x1f1\x1f80\x1f24\n", "", 0),
        ]
    )
    window = Server(server.path).session.new_window(name="build")
    assert window.id == "@4"
    assert window.index == 2
    assert window.name == "build"
    assert server.asked[1].startswith("new-window -n build -P -F ")


def test_the_session_of_a_server(fake):
    server = fake([("$0\x1fwork\x1f1\x1f3\x1f/home\x1f0\n", "", 0)])
    session = Server(server.path).session
    assert session.id == "$0"
    assert session.name == "work"
    assert session.attached == 1
    assert session.path == "/home"


def test_a_server_with_no_session_says_so(fake):
    server = fake([("", "", 0)])
    with pytest.raises(LookupError):
        Server(server.path).session


def test_finding_a_pane_by_id(fake):
    rows = pane_row(pane_id="%1") + "\n" + pane_row(pane_id="%2") + "\n"
    server = fake([(rows, "", 0), (rows, "", 0)])
    server_object = Server(server.path)
    assert server_object.pane("%2").id == "%2"
    assert server_object.pane("%99") is None


# ----------------------------------------------------------------------
# Quoting.


@pytest.mark.parametrize(
    "argument, expected",
    [
        ("plain", "plain"),
        ("two words", "'two words'"),
        ("it's", "'it'\"'\"'s'"),
        ("$HOME", "'$HOME'"),
    ],
)
def test_quote(argument, expected):
    assert quote(argument) == expected
