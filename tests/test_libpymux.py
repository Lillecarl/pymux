"""
libpymux without a server.

These cover what the library does with what a server says: the format
strings it asks for, the rows it reads back, the commands it builds and
the quoting it puts around them. `drive_with_pty.py` runs the same
library against a server that is really there.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading

import pytest
from libpymux import (
    CommandError,
    Connection,
    Job,
    Pane,
    Server,
    ServerNotRunning,
    Window,
    operations,
    quote,
)
from libpymux.connection import CommandResult, Reply, command_packet
from libpymux.operations import PANE_FIELDS, SEPARATOR, JobRecord, format_string
from libpymux.protocol import Framer

# ----------------------------------------------------------------------
# A server that says what a test tells it to say.


class FakeServer:
    """
    A unix socket that answers one command and closes, the way the
    real server does.
    """

    def __init__(self, path, answers):
        self.path = str(path)
        self.answers = list(answers)
        self.asked = []
        self.packets = []
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
                self.packets.append(packet)

                answer = self.answers.pop(0) if self.answers else ("", "", 0)
                out, err, code = answer
                for kind, text in (("out", out), ("err", err)):
                    if text:
                        connection.sendall(json.dumps({"cmd": kind, "data": text}).encode() + b"\0")
                connection.sendall(json.dumps({"cmd": "exit", "code": code}).encode() + b"\0")

    def close(self):
        self.socket.close()


@pytest.fixture
def fake(tmp_path):
    servers = []

    def make(answers=()):
        server = FakeServer(tmp_path / ("fake%d.sock" % len(servers)), answers)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def pane_row(**values):
    "One line of `list-panes -F`, with the fields this library asks for."
    defaults = {name: "" for name in PANE_FIELDS}
    defaults.update(values)
    return SEPARATOR.join(defaults[name] for name in PANE_FIELDS)


# ----------------------------------------------------------------------
# The wire.


async def test_command_reaches_server(fake):
    server = fake([("hello\n", "", 0)])
    result = await Server(server.path).cmd("list-windows")
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    assert server.asked == ["list-windows"]


async def test_list_of_arguments_is_quoted(fake):
    server = fake([("", "", 0)])
    await Server(server.path).cmd(["send-keys", "-l", "two words"])
    assert server.asked == ["send-keys -l 'two words'"]


async def test_failing_command_raises(fake):
    server = fake([("", "no such window\n", 1)])
    with pytest.raises(CommandError) as raised:
        await Server(server.path).cmd("select-window -t 99")
    assert "no such window" in str(raised.value)
    assert raised.value.result.exit_code == 1


async def test_failing_command_can_be_asked_not_to_raise(fake):
    server = fake([("", "nope\n", 1)])
    result = await Server(server.path).cmd("has-session -t x", check=False)
    assert not result.ok
    assert result.stderr == "nope\n"


async def test_has_session_reads_exit_code(fake):
    server = fake([("", "", 0), ("", "can't find session\n", 1)])
    server_object = Server(server.path)
    assert await server_object.has_session("here")
    assert not await server_object.has_session("gone")


async def test_server_that_is_not_there(tmp_path):
    connection = Connection(str(tmp_path / "nothing.sock"))
    assert not await connection.is_alive()
    assert not await Server(connection.socket_path).is_alive()
    with pytest.raises(ServerNotRunning):
        await connection.run("list-sessions")


async def test_a_command_says_who_sent_it(fake):
    "A job then starts where the caller stands, stamped with its session."
    server = fake()
    environment = {"OPENCODE_SESSION_ID": "agent-1", "HOME": "/home/someone"}
    await Server(server.path, environment=environment, cwd="/work").cmd("list-sessions")
    (packet,) = server.packets
    assert packet["environment"] == {**environment, "PYMUX_AGENTIC_ID": "agent-1"}
    assert packet["cwd"] == "/work"


async def test_a_command_says_this_process_sent_it_by_default(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("PYMUX_AGENTIC_ID", "this-one")
    monkeypatch.chdir(tmp_path)
    server = fake()
    await Connection(server.path).run("list-sessions")
    (packet,) = server.packets
    assert packet["environment"]["PYMUX_AGENTIC_ID"] == "this-one"
    assert packet["environment"]["HOME"] == os.environ["HOME"]
    assert packet["cwd"] == str(tmp_path)


def test_a_reply_cut_across_reads_comes_out_whole():
    reply = Reply()
    data = b"".join(
        json.dumps(packet).encode() + b"\0"
        for packet in (
            {"cmd": "out", "data": "one\n"},
            {"cmd": "err", "data": "warn\n"},
            {"cmd": "out", "data": "two\n"},
            {"cmd": "exit", "code": 3},
        )
    )
    for index in range(0, len(data), 5):
        reply.feed(data[index : index + 5])
    assert reply.result() == CommandResult("one\ntwo\n", "warn\n", 3)


def test_a_command_packet_names_its_pane():
    packet = command_packet("list-panes", "%1001")
    assert packet.endswith(b"\0")
    assert json.loads(packet[:-1]) == {"cmd": "run-command", "data": "list-panes", "pane_id": "%1001"}


# ----------------------------------------------------------------------
# The format strings, and the rows that come back.


def test_format_string_asks_for_every_field():
    text = format_string(["pane_id", "pane_index"])
    assert text == "#{pane_id}" + SEPARATOR + "#{pane_index}"


def test_separator_is_not_something_terminal_writes():
    # A tab or a space would come back inside a pane title.
    assert SEPARATOR not in " \t|:,;"


def test_row_becomes_dictionary():
    rows = operations.rows(CommandResult("a\x1fb\n", "", 0), ["one", "two"])
    assert rows == [{"one": "a", "two": "b"}]


def test_short_row_fills_fields_it_misses():
    "An older server knows fewer variables, and answers with fewer."
    rows = operations.rows(CommandResult("a\n", "", 0), ["one", "two"])
    assert rows == [{"one": "a", "two": ""}]


def test_empty_output_is_no_rows():
    assert operations.rows(CommandResult("", "", 0), ["one"]) == []


# ----------------------------------------------------------------------
# The objects.


async def test_panes_of_server(fake):
    rows = "\n".join(
        [
            pane_row(pane_id="%1", pane_index="0", window_id="@0", pane_active="1"),
            pane_row(pane_id="%2", pane_index="1", window_id="@0", pane_active="0"),
        ]
    )
    server = fake([(rows + "\n", "", 0)])
    panes = await Server(server.path).panes()
    assert [pane.id for pane in panes] == ["%1", "%2"]
    assert [pane.index for pane in panes] == [0, 1]
    assert [pane.active for pane in panes] == [True, False]
    assert server.asked[0].startswith("list-panes -a -F ")


async def test_pane_reads_its_numbers(fake):
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
    (pane,) = await Server(server.path).panes()
    assert (pane.width, pane.height) == (80, 24)
    assert pane.pid == 4242
    assert pane.title == "a title"
    assert pane.current_command == "fish"
    assert not pane.dead


async def test_a_pane_says_which_mode_is_over_it(fake):
    "And `in_mode` counts every mode, not copy mode alone."
    row = pane_row(pane_id="%7", pane_in_mode="1", pane_mode="clock-mode")
    server = fake([(row + "\n", "", 0)])
    (pane,) = await Server(server.path).panes()
    assert pane.in_mode
    assert pane.mode == "clock-mode"


async def test_field_that_server_left_empty_is_not_crash(fake):
    server = fake([(pane_row(pane_id="%1") + "\n", "", 0)])
    (pane,) = await Server(server.path).panes()
    assert pane.width == 0
    assert pane.pid == -1
    assert pane.index == -1


async def test_panes_of_window_are_ones_it_holds(fake):
    rows = "\n".join(
        [
            pane_row(pane_id="%1", window_id="@0"),
            pane_row(pane_id="%2", window_id="@1"),
            pane_row(pane_id="%3", window_id="@1"),
        ]
    )
    server = fake([(rows + "\n", "", 0)])
    window = Window(Server(server.path), {"window_id": "@1"})
    assert [pane.id for pane in await window.panes()] == ["%2", "%3"]


async def test_send_keys_sends_text_and_then_return(fake):
    server = fake([("", "", 0), ("", "", 0)])
    await Pane(Server(server.path), {"pane_id": "%5"}).send_keys("echo hi")
    assert server.asked == [
        "send-keys -t %5 -l 'echo hi'",
        "send-keys -t %5 Enter",
    ]


async def test_send_keys_without_return(fake):
    server = fake([("", "", 0)])
    await Pane(Server(server.path), {"pane_id": "%5"}).send_keys("hi", enter=False)
    assert server.asked == ["send-keys -t %5 -l hi"]


async def test_send_keys_can_name_key(fake):
    server = fake([("", "", 0)])
    await Pane(Server(server.path), {"pane_id": "%5"}).send_key("C-c")
    assert server.asked == ["send-keys -t %5 C-c"]


async def test_capture_asks_for_lines_it_was_given(fake):
    server = fake([("one\ntwo\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%5"})
    assert await pane.capture(start=-10, end=0) == "one\ntwo\n"
    assert server.asked == ["capture-pane -p -t %5 -S -10 -E 0"]


async def test_capture_without_lines_asks_for_screen(fake):
    server = fake([("", "", 0)])
    await Pane(Server(server.path), {"pane_id": "%5"}).capture()
    assert server.asked == ["capture-pane -p -t %5"]


def test_the_revision_of_a_pane_is_the_one_it_was_read_at(fake):
    "The snapshot, like every other field."
    server = fake([])
    pane = Pane(Server(server.path), {"pane_id": "%5", "pane_revision": "42"})
    assert pane.revision == 42
    assert server.asked == []


async def test_asking_for_the_revision_now_goes_to_the_server(fake):
    """
    The trap this exists for: a caller that keeps its `Pane` would seed a
    wait with the number from when the object was made, so every wait
    answers at once and the loop spins.
    """
    server = fake([(pane_row(pane_id="%5", pane_revision="99") + "\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%5", "pane_revision": "1"})

    assert await pane.current_revision() == 99
    assert server.asked, "it has to ask the server"


async def test_a_pane_that_is_gone_says_so(fake):
    server = fake([(pane_row(pane_id="%1") + "\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%7"})
    with pytest.raises(LookupError):
        await pane.current_revision()


async def test_waiting_for_a_change_names_what_it_saw(fake):
    server = fake([("100\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%5"})
    assert await pane.wait_for_change(since=3, timeout=1.5) == 100
    assert server.asked == ["wait-pane-change -t %5 --since 3 --timeout 1.5"]


async def test_capture_html_asks_for_the_html_of_a_pane(fake):
    server = fake([("<pre></pre>\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%5"})
    assert await pane.capture_html() == "<pre></pre>\n"
    assert server.asked == ["capture-pane -p -H -t %5"]


async def test_capture_html_asks_for_lines_it_was_given(fake):
    server = fake([("", "", 0)])
    await Pane(Server(server.path), {"pane_id": "%5"}).capture_html(start=-10, end=0)
    assert server.asked == ["capture-pane -p -H -t %5 -S -10 -E 0"]


async def test_a_picture_names_its_fonts_and_cell_and_answers_bytes(fake):
    server = fake([(base64.b64encode(b"\x89PNG").decode() + "\n", "", 0)])
    pane = Pane(Server(server.path), {"pane_id": "%5"})
    assert await pane.capture_png(fonts=["/f/a.ttf", "/f/b.ttf"], cell=(9, 18)) == b"\x89PNG"
    assert server.asked == ["capture-pane -p --png --font /f/a.ttf --font /f/b.ttf --cell 9x18 -t %5"]


async def test_a_client_is_captured_by_its_name_or_the_one_used_last(fake):
    server = fake([("frame\n", "", 0), (base64.b64encode(b"\x89PNG").decode() + "\n", "", 0)])
    connected = Server(server.path)
    assert await connected.capture_client() == "frame\n"
    assert await connected.capture_client_png("desk", cell=(9, 18), fit=800) == b"\x89PNG"
    assert server.asked == ["capture-client -p", "capture-client -p --png --fit 800 --cell 9x18 -t desk"]


async def test_compact_html_asks_for_the_spelling_a_reader_reads(fake):
    server = fake([("", "", 0)])
    await Pane(Server(server.path), {"pane_id": "%5"}).capture_html(compact=True)
    assert server.asked == ["capture-pane -p -H --compact -t %5"]


async def test_the_stylesheet_of_no_pane_names_none(fake):
    server = fake([(".pyte-screen { }\n", "", 0)])
    assert await Server(server.path).html_stylesheet() == ".pyte-screen { }\n"
    assert server.asked == ["show-html-stylesheet"]


async def test_the_stylesheet_of_one_pane_names_it(fake):
    server = fake([("", "", 0)])
    pymux_server = Server(server.path)
    pane = Pane(pymux_server, {"pane_id": "%5"})
    await pymux_server.html_stylesheet(pane)
    assert server.asked == ["show-html-stylesheet -t %5"]


async def test_splitting_window_returns_new_pane(fake):
    server = fake([(pane_row(pane_id="%9", pane_index="1") + "\n", "", 0)])
    window = Window(Server(server.path), {"window_id": "@0", "window_index": "0"})
    pane = await window.split(command="fish")
    assert pane.id == "%9"
    assert pane.server is window.server
    asked = server.asked[0]
    assert asked.startswith("split-window -t @0 -v ")
    assert asked.endswith(" fish")
    assert " -P -F " in asked


async def test_splitting_other_way(fake):
    server = fake([("", "", 0)])
    window = Window(Server(server.path), {"window_id": "@0", "window_index": "0"})
    assert await window.split(vertical=False) is None
    assert server.asked[0].startswith("split-window -t @0 -h ")


async def test_new_window_carries_its_name(fake):
    # Reading the session asks first, and making the window asks second.
    server = fake(
        [
            ("$0\x1fwork\x1f1\x1f1\x1f/home\x1f0\n", "", 0),
            ("@4\x1f2\x1fbuild\x1f1\x1f*\x1f1\x1f80\x1f24\n", "", 0),
        ]
    )
    session = await Server(server.path).session()
    window = await session.new_window(name="build")
    assert window.id == "@4"
    assert window.index == 2
    assert window.name == "build"
    # The session names itself, so the window lands in it and not in
    # whichever session a client is on. Lillecarl/pymux#323.
    assert server.asked[1].startswith("new-window -t work: -n build -P -F ")


async def test_session_of_server(fake):
    server = fake([("$0\x1fwork\x1f1\x1f3\x1f/home\x1f0\n", "", 0)])
    session = await Server(server.path).session()
    assert session.id == "$0"
    assert session.name == "work"
    assert session.attached == 1
    assert session.path == "/home"


async def test_server_with_no_session_says_so(fake):
    server = fake([("", "", 0)])
    with pytest.raises(LookupError):
        await Server(server.path).session()


async def test_finding_pane_by_id(fake):
    rows = pane_row(pane_id="%1") + "\n" + pane_row(pane_id="%2") + "\n"
    server = fake([(rows, "", 0), (rows, "", 0)])
    server_object = Server(server.path)
    assert (await server_object.pane("%2")).id == "%2"
    assert await server_object.pane("%99") is None


async def test_killing_the_server_does_not_raise_on_its_exit_code(fake):
    server = fake([("", "", 1)])
    await Server(server.path).kill()
    assert server.asked == ["kill-server"]


# ----------------------------------------------------------------------
# Jobs.


def job_rows(*jobs):
    "What `sql --json` answers for the jobs table."
    defaults = {
        "command": "true",
        "directory": None,
        "status": "done",
        "returncode": 0,
        "error": None,
        "started": 1.0,
        "finished": 2.0,
        "pty": 0,
    }
    return (json.dumps({"columns": [], "rows": [{**defaults, **job} for job in jobs], "truncated": False}), "", 0)


def tag_rows(*tags):
    rows = [{"job_id": job_id, "key": key, "value": value} for job_id, key, value in tags]
    return (json.dumps({"columns": [], "rows": rows, "truncated": False}), "", 0)


async def test_running_a_job_names_everything_it_was_given(fake):
    server = fake([("7\n", "", 0), job_rows({"id": 7, "status": "running"}), tag_rows((7, "deploy", None))])
    job = await Server(server.path).run_job(
        "make; make install",
        tags={"deploy": None, "branch": "main"},
        directory="/src",
        env={"CC": "clang"},
        pty=True,
    )
    assert server.asked[0] == (
        "run --tag deploy --tag branch=main -d /src --env CC=clang --pty -- 'make; make install'"
    )
    assert (job.id, job.running, job.tags) == (7, True, {"deploy": None})


async def test_jobs_are_read_with_their_tags_and_filtered_by_them(fake):
    server = fake(
        [
            job_rows({"id": 1}, {"id": 2, "pty": 1}, {"id": 3}),
            tag_rows((1, "deploy", None), (2, "deploy", "prod"), (2, "session", "agent-1"), (3, "other", None)),
        ]
    )
    jobs = await Server(server.path).jobs({"deploy": None})
    assert [job.id for job in jobs] == [1, 2]
    assert jobs[1].record.pty
    assert jobs[1].tags == {"deploy": "prod", "session": "agent-1"}
    assert server.asked[0].startswith("sql --json -n 0 ")


async def test_a_tag_with_a_value_matches_that_value_alone(fake):
    server = fake([job_rows({"id": 1}, {"id": 2}), tag_rows((1, "deploy", "prod"), (2, "deploy", "test"))])
    assert [job.id for job in await Server(server.path).jobs({"deploy": "test"})] == [2]


async def test_a_job_that_exits_124_is_not_a_job_still_running(fake):
    "`pymux tail` answers 124 for both; the record read after says which."
    server = fake([("out\n", "", 124), job_rows({"id": 5, "returncode": 124}), tag_rows()])
    job = Job(Server(server.path), _record(5, "running"))
    assert await job.tail(timeout=1, lines=0) == "out\n"
    assert not job.running
    assert job.returncode == 124
    assert server.asked[0] == "tail 5 --lines 0 --timeout 1"


async def test_a_tail_past_its_timeout_leaves_the_job_running(fake):
    server = fake([("", "", 124), job_rows({"id": 5, "status": "running", "returncode": None}), tag_rows()])
    job = Job(Server(server.path), _record(5, "running"))
    await job.tail(timeout=1)
    assert job.running


async def test_tailing_a_job_the_server_forgot_says_so(fake):
    server = fake([("", "no job 5\n", 4), job_rows(), tag_rows()])
    with pytest.raises(LookupError):
        await Job(Server(server.path), _record(5, "running")).tail(timeout=1)


@pytest.mark.parametrize(("kill", "asked"), [(False, "view-job -t %3 5"), (True, "view-job -k -t %3 5")])
async def test_viewing_a_job_replaces_the_pane_it_names(fake, kill, asked):
    server = fake([("", "", 0)])
    connected = Server(server.path)
    await Job(connected, _record(5, "running")).view(Pane(connected, {"pane_id": "%3"}), kill=kill)
    assert server.asked == [asked]


def test_the_enclosing_pane_is_the_one_pymux_names_in_the_environment():
    server, pane = Server.enclosing({"PYMUX": "/run/user/1/pymux.sock.a,b,1001"})
    assert (server.socket_path, pane.id) == ("/run/user/1/pymux.sock.a,b", "%1001")


@pytest.mark.parametrize("value", ["", "/run/pymux.sock", "/run/pymux.sock,", ",5", "/run/pymux.sock,%5"])
def test_outside_a_pane_nothing_encloses(value):
    assert Server.enclosing({"PYMUX": value} if value else {}) is None


def _record(job_id, status):
    return JobRecord(job_id, "true", None, status, None, None, 1.0, None, False, {})


# ----------------------------------------------------------------------
# Quoting.


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("plain", "plain"),
        ("two words", "'two words'"),
        ("it's", "'it'\"'\"'s'"),
        ("$HOME", "'$HOME'"),
    ],
)
def test_quote(argument, expected):
    assert quote(argument) == expected


def test_a_message_cut_across_reads_comes_out_whole_and_once():
    "Every side of the wire reads through one `Framer`."
    framer = Framer()

    assert framer.feed(b'{"a"') == []
    assert framer.feed(b": 1}\0{") == [b'{"a": 1}']
    assert framer.feed(b"}\0\0x") == [b"{}", b""]
    assert framer.feed(b"\0") == [b"x"]


def test_a_message_of_megabytes_read_a_kilobyte_at_a_time_comes_out_whole():
    "A window pane's frame, the way the client reads it."
    framer = Framer()
    message = bytes(range(1, 256)) * 8000
    stream = message + b"\0" + b"after\0"
    got = []
    for at in range(0, len(stream), 1024):
        got += framer.feed(stream[at : at + 1024])
    assert got == [message, b"after"]
