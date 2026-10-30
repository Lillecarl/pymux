"""
A file that a pane asks for from the person's machine: `pymux receive`.
The pymux client there asks the person, holds the grant, and reads its
allow-list from its own configuration file. Lillecarl/pymux#743.
"""

from __future__ import annotations

import argparse
import base64
import shlex
import sys
from pathlib import Path

import anyio
import pytest
from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.client import uploads
from pymux.client.terminal import BUSY, SAID_NO, TerminalClient
from pymux.client.uploads import (
    Answer,
    Wanted,
    allowed,
    answer_of,
    inspect,
    listing,
    outcome_packets,
    read,
    size_text,
    upload_settings,
)
from pymux.commands.receive import wait_for_files
from pymux.connection.uploads import LIST_SECONDS, Upload, receive_into
from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)

#: A pane that is still there when the test looks at it.
PANE_THAT_STAYS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

DATA = bytes(range(256)) * 40


class Clock:
    "A clock the test moves."

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class AskedClient(TerminalClient):
    """
    A terminal client with no terminal: it keeps what it would send to
    the server and what it would draw.
    """

    def __init__(self, config: Path, send=None) -> None:
        super().__init__()
        self.config_file = str(config)
        self.sent: list[dict] = []
        self.drawn: list[bytes] = []
        self._send = send
        self.clock = Clock()
        self._clock = self.clock

    def _send_packet(self, data) -> None:
        self.sent.append(data)
        if self._send is not None:
            self._send(data)

    def _write_terminal(self, data: bytes) -> None:
        self.drawn.append(data)

    def size(self):
        return (SIZE.rows, SIZE.columns)

    def of(self, kind: Packet) -> list[dict]:
        return [packet for packet in self.sent if packet[Field.CMD] == kind]

    def done(self, request: str) -> dict | None:
        for packet in self.of(Packet.UPLOAD_DONE):
            if packet[Field.REQUEST] == request:
                return packet
        return None

    def uploaded(self, request: str) -> bytes:
        parts = [packet for packet in self.of(Packet.UPLOAD) if packet[Field.REQUEST] == request]
        return b"".join(base64.b64decode(packet.get(Field.DATA, "")) for packet in parts)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    "The person's home directory, where the client resolves a path."
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def config(tmp_path) -> Path:
    "The client's own configuration file, empty until a test writes it."
    path = tmp_path / "client.conf"
    path.write_text("")
    return path


def ask(client: AskedClient, paths: list[str], request: str) -> None:
    "The server's `send-files`, as the client reads it."
    client._process({Field.CMD: Packet.SEND_FILES, Field.REQUEST: request, Field.PATHS: paths, Field.PANE: "%3"})


async def until_asked_or_done(client: AskedClient, request: str) -> None:
    await once(
        lambda: client._asking is not None or client.done(request) is not None,
        10.0,
        lambda: "the client neither asked nor answered; it sent %r" % (client.sent,),
    )


async def until_done(client: AskedClient, request: str) -> dict:
    return await once(
        lambda: client.done(request),
        10.0,
        lambda: "the client never finished; it sent %r" % (client.sent,),
    )


# ----------------------------------------------------------------------
# The question and its answers.


async def test_y_sends_once_and_asks_again(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")

        # Asked, and nothing read or sent before the answer.
        assert client._asking is not None
        assert client.of(Packet.UPLOAD) == []
        drawn = b"".join(client.drawn).decode()
        assert "pane %3" in drawn
        assert "~/x.pdf (10.2 kB)" in drawn
        assert ": y once / a for 10 min / n" in drawn
        # On the last row, with the cursor put back.
        assert drawn.startswith("\x1b7\x1b[24;1H\x1b[2K")
        assert drawn.endswith("\x1b8")

        client._take_input("y")
        done = await until_done(client, "one")
        assert Field.ERROR not in done
        assert client.uploaded("one") == DATA
        # The question is gone, and the server draws over where it was.
        assert client.of(Packet.REDRAW)
        assert client._asking is None

        ask(client, ["~/x.pdf"], "two")
        await until_asked_or_done(client, "two")
        assert client._asking is not None
        tasks.cancel_scope.cancel()


async def test_n_sends_nothing(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")
        client._take_input("n")
        done = await until_done(client, "one")
        assert done[Field.ERROR] == SAID_NO
        assert client.of(Packet.UPLOAD) == []


async def test_a_grants_this_server_until_it_ends(home, config):
    (home / "x.pdf").write_bytes(DATA)
    config.write_text("set-client-option upload-grant 5\n")
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")
        client._take_input("a")
        await until_done(client, "one")

        # Inside the grant: no question.
        client.clock.now += 5 * 60 - 1
        ask(client, ["~/x.pdf"], "two")
        await until_asked_or_done(client, "two")
        assert client._asking is None
        await until_done(client, "two")
        assert client.uploaded("two") == DATA

        # Past it: the question again.
        client.clock.now += 2
        ask(client, ["~/x.pdf"], "three")
        await until_asked_or_done(client, "three")
        assert client._asking is not None
        tasks.cancel_scope.cancel()


async def test_no_key_reaches_the_server_while_it_asks(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")

        # Neither a key that is no answer nor fast typing that starts
        # with one answers, or reaches the pane.
        client._take_input("x")
        client._take_input("yes")
        assert client._asking is not None
        assert client.of(Packet.IN) == []

        # A sequence the terminal answers with goes on: the server
        # waits for its status reports.
        client._take_input("\x1b[0n")
        assert client.of(Packet.IN) == [{Field.CMD: Packet.IN, Field.DATA: "\x1b[0n"}]
        assert client._asking is not None

        # The keys reach the server again after the answer.
        client._take_input("\x1b")
        await until_done(client, "one")
        client._take_input("x")
        assert client.of(Packet.IN)[-1] == {Field.CMD: Packet.IN, Field.DATA: "x"}


async def test_a_second_request_while_asking_is_refused(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")
        ask(client, ["~/x.pdf"], "two")
        done = await until_done(client, "two")
        assert done[Field.ERROR] == BUSY
        tasks.cancel_scope.cancel()


async def test_a_frame_does_not_wipe_the_question(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")
        client.drawn.clear()
        client._process({Field.CMD: Packet.OUT, Field.DATA: "a frame"})
        assert client.drawn[0] == b"a frame"
        assert b"y once" in client.drawn[-1]
        tasks.cancel_scope.cancel()


async def test_a_withdrawn_request_takes_its_question_down(home, config):
    "A later y is a key for the pane, and sends no file nobody takes."
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/x.pdf"], "one")
        await until_asked_or_done(client, "one")
        client._process({Field.CMD: Packet.FILES_WITHDRAWN, Field.REQUEST: "one"})
        assert client._asking is None
        assert client.of(Packet.REDRAW)
        client._take_input("y")
        assert client.of(Packet.IN) == [{Field.CMD: Packet.IN, Field.DATA: "y"}]
        assert client.of(Packet.UPLOAD) == []
        tasks.cancel_scope.cancel()


def test_a_request_withdrawn_while_looked_at_is_never_asked(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    looks = []
    client._off_the_loop = lambda work, then: looks.append((work, then))
    ask(client, ["~/x.pdf"], "one")
    client._process({Field.CMD: Packet.FILES_WITHDRAWN, Field.REQUEST: "one"})
    [(work, then)] = looks
    then(work())
    assert client._asking is None
    assert client.done("one") is None


async def test_a_stopped_receive_withdraws_its_request():
    sent: list[dict] = []
    async with over_connection(read_packet=sent.append) as session:
        state, _ = await session.attach("asked", SIZE, forwards=True)
        connection = state.connection
        upload = connection.ask_for_files(["x"], "%1")
        with anyio.move_on_after(0.1):
            await wait_for_files(session.pymux, connection, upload, Path("/"))
        await once(
            lambda: any(packet["cmd"] == Packet.FILES_WITHDRAWN for packet in sent),
            10.0,
            lambda: "the server never withdrew the request; it sent %r" % ([packet["cmd"] for packet in sent],),
        )
        assert upload.request not in connection.uploads


def test_the_keyboard_flags_are_off_while_it_asks(home, config):
    "A key with the flags on arrives as an escape, which the question passes on."
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    client._kitty_supported = True
    client._set_kitty_flags(31)
    client._decide("one", "%3", upload_settings([], home), inspect(["~/x.pdf"], home))
    assert client._kitty_flags == 0
    client._process({Field.CMD: Packet.KITTY_KEYBOARD, Field.DATA: {"flags": 5}})
    assert client._kitty_flags == 0
    client._answer("n")
    assert client._kitty_flags == 5


@pytest.mark.parametrize(
    ("keys", "answer"),
    [
        ("y", Answer.ONCE),
        ("Y", Answer.ONCE),
        ("a", Answer.GRANT),
        ("n", Answer.NO),
        ("\x1b", Answer.NO),
        ("\x03", Answer.NO),
        ("x", None),
        ("ya", None),
        ("", None),
    ],
)
def test_the_answer_of_a_key(keys, answer):
    assert answer_of(keys) is answer


def test_the_question_shows_nothing_the_terminal_acts_on(home, config):
    (home / "x.pdf").write_bytes(DATA)
    client = AskedClient(config)
    client._decide("one", "%3\x1b]0;evil\x07", upload_settings([], home), inspect(["~/x.pdf"], home))
    assert client._asking is not None
    assert "\x1b" not in client._asking.text
    assert "\x07" not in client._asking.text


@pytest.mark.parametrize(("size", "text"), [(0, "0 B"), (999, "999 B"), (2_100_000, "2.1 MB"), (10_240, "10.2 kB")])
def test_a_size_for_a_person(size, text):
    assert size_text(size) == text


# ----------------------------------------------------------------------
# The allow-list.


async def test_a_path_under_upload_paths_passes_without_asking(home, config):
    shared = home / "shared"
    shared.mkdir()
    (shared / "x.pdf").write_bytes(DATA)
    (home / "elsewhere.pdf").write_bytes(DATA)
    config.write_text("set-client-option upload-paths ~/shared\n")
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        ask(client, ["~/shared/x.pdf"], "hit")
        await until_asked_or_done(client, "hit")
        assert client._asking is None
        await until_done(client, "hit")
        assert client.uploaded("hit") == DATA

        ask(client, ["~/elsewhere.pdf"], "miss")
        await until_asked_or_done(client, "miss")
        assert client._asking is not None
        tasks.cancel_scope.cancel()


@pytest.mark.parametrize("asked", ["~/shared/link", "~/shared/../secret.txt", "shared/../secret.txt"])
def test_an_escape_from_upload_paths_is_asked(home, asked):
    shared = home / "shared"
    shared.mkdir()
    (home / "secret.txt").write_bytes(b"secret")
    (shared / "link").symlink_to(home / "secret.txt")
    roots = upload_settings([("upload-paths", "~/shared")], home).roots
    wanted = inspect([asked], home)
    assert wanted[0].refused is None
    assert wanted[0].path == home / "secret.txt"
    assert not allowed(wanted, roots)


def test_a_symlinked_root_still_holds_its_own_files(home, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "x").write_bytes(b"x")
    (home / "shared").symlink_to(real)
    roots = upload_settings([("upload-paths", "~/shared")], home).roots
    assert allowed(inspect(["~/shared/x"], home), roots)


def test_a_grant_lists_a_whole_directory(home):
    (home / "dir").mkdir()
    (home / "x.pdf").write_bytes(b"x")
    assert listing("~", home, (), granted=True) == ["dir/", "x.pdf"]
    assert listing("", home, (), granted=True) == ["dir/", "x.pdf"]


def test_without_a_grant_only_the_way_into_upload_paths_lists(home):
    shared = home / "a" / "shared"
    shared.mkdir(parents=True)
    (shared / "x.pdf").write_bytes(b"x")
    (home / "a" / "private.txt").write_bytes(b"x")
    (home / "secret.txt").write_bytes(b"x")
    roots = upload_settings([("upload-paths", "~/a/shared")], home).roots
    assert listing("~", home, roots, granted=False) == ["a/"]
    assert listing("~/a/", home, roots, granted=False) == ["shared/"]
    assert listing("~/a/shared", home, roots, granted=False) == ["x.pdf"]
    assert listing("~", home, (), granted=False) == []


def test_a_missing_directory_lists_nothing(home):
    assert listing("~/missing", home, (), granted=True) == []


async def test_the_client_answers_a_listing_without_asking(home, config):
    (home / "x.pdf").write_bytes(DATA)
    config.write_text("set-client-option upload-paths ~\n")
    client = AskedClient(config)
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        client._process({Field.CMD: Packet.LIST_FILES, Field.REQUEST: "one", Field.PATH: "~"})
        [answered] = await once(
            lambda: client.of(Packet.FILES_LISTED), 10.0, lambda: "no listing; sent %r" % (client.sent,)
        )
        assert answered == {Field.CMD: Packet.FILES_LISTED, Field.REQUEST: "one", Field.PATHS: ["x.pdf"]}
        assert client._asking is None


async def test_the_server_waits_for_a_listing_then_gives_up():
    async with over_connection() as session:
        state, _ = await session.attach("listed", SIZE, forwards=True)
        connection = state.connection

        async def answer_soon() -> None:
            await once(lambda: connection.listings, 10.0, lambda: "never asked")
            [request] = connection.listings
            connection._process({Field.CMD: Packet.FILES_LISTED, Field.REQUEST: request, Field.PATHS: ["a/", "b"]})

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(answer_soon)
            assert await connection.list_files("~") == ["a/", "b"]
        assert connection.listings == {}

        # Nobody answers this one.
        with anyio.fail_after(LIST_SECONDS + 5):
            assert await connection.list_files("~") == []
        assert connection.listings == {}


def test_upload_paths_takes_several(home):
    settings = upload_settings([("upload-paths", "~/a:/srv/b")], home)
    assert settings.roots == (home / "a", Path("/srv/b"))


async def answer_to(session, command: str, pane_id=None) -> tuple[str, str]:
    "What a command over the socket printed, on stdout and on stderr."
    got = await session.command(command, pane_id=pane_id)
    await once(lambda: any(packet["cmd"] == "exit" for packet in got), 10.0, lambda: "no answer: %r" % (got,))
    return (
        "".join(packet["data"] for packet in got if packet["cmd"] == "out"),
        "".join(packet["data"] for packet in got if packet["cmd"] == "err"),
    )


@pytest.mark.parametrize("option", ["upload-paths /", "upload-grant 100000"])
async def test_the_server_cannot_set_an_upload_option(option):
    async with over_connection() as session:
        await session.attach("one", SIZE)
        _out, errors = await answer_to(session, "set-client-option %s" % (option,))
        assert "client's own machine" in errors


def test_the_client_never_tells_the_server_its_allow_list(config):
    config.write_text(
        "set-client-option theme nord\nset-client-option upload-paths /\nset-client-option upload-grant 60\n"
    )
    assert AskedClient(config)._client_options() == [("theme", "nord")]


# ----------------------------------------------------------------------
# What is read.


def test_a_file_too_large_is_not_read(home, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_DOWNLOAD_BYTES", 100)
    (home / "big").write_bytes(bytes(101))
    [wanted] = inspect(["~/big"], home)
    assert wanted.refused is not None
    assert "larger than" in wanted.refused
    [packet] = outcome_packets("one", wanted)
    assert "larger than" in packet[Field.ERROR]
    assert Field.DATA not in packet


def test_a_file_that_grew_past_the_bound_is_not_sent(home, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_DOWNLOAD_BYTES", 100)
    (home / "grows").write_bytes(bytes(10))
    [wanted] = inspect(["~/grows"], home)
    (home / "grows").write_bytes(bytes(101))
    with pytest.raises(uploads.NotSent, match="larger than"):
        read(wanted)


def test_a_file_swapped_after_the_question_is_not_read(home):
    (home / "x").write_bytes(b"asked about")
    [wanted] = inspect(["~/x"], home)
    (home / "x").unlink()
    (home / "x").write_bytes(b"something else")
    with pytest.raises(uploads.NotSent, match="changed"):
        read(wanted)


def test_a_directory_or_a_missing_path_is_not_sent(home):
    (home / "dir").mkdir()
    missing, directory = inspect(["~/missing", "~/dir"], home)
    assert missing.refused is not None
    assert directory.refused == "~/dir is not a file"


# ----------------------------------------------------------------------
# The server's half.


def parts_of(request: str, path: Path, asked: str) -> list[dict]:
    wanted = Wanted(asked, path, path.stat().st_size, (path.stat().st_dev, path.stat().st_ino))
    return list(outcome_packets(request, wanted))


async def test_the_server_never_writes_over_a_file(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"new")
    directory = tmp_path / "into"
    directory.mkdir()
    (directory / "report.txt").write_bytes(b"there first")

    upload = Upload(["report.txt"])
    for packet in parts_of(upload.request, source, "report.txt"):
        upload.take(packet)
    upload.take({Field.CMD: Packet.UPLOAD_DONE, Field.REQUEST: upload.request})
    said: list[str] = []
    assert await receive_into(upload, directory, said.append) == []
    assert (directory / "report.txt").read_bytes() == b"there first"
    assert (directory / "report (1).txt").read_bytes() == b"new"
    assert said == ["Saved %s." % (directory / "report (1).txt",)]


async def test_the_server_bounds_what_arrives(tmp_path, monkeypatch):
    from pymux import files

    monkeypatch.setattr(files, "MAX_DOWNLOAD_BYTES", 10)
    source = tmp_path / "big"
    source.write_bytes(bytes(11))
    upload = Upload(["big"])
    for packet in parts_of(upload.request, source, "big"):
        upload.take(packet)
    upload.take({Field.CMD: Packet.UPLOAD_DONE, Field.REQUEST: upload.request})
    [why] = await receive_into(upload, tmp_path, lambda _text: None)
    assert "larger than" in why


async def test_a_path_never_answered_is_said(tmp_path):
    upload = Upload(["a", "b"])
    upload.take({Field.CMD: Packet.UPLOAD, Field.REQUEST: upload.request, Field.PATH: "a", Field.ERROR: "no"})
    upload.take({Field.CMD: Packet.UPLOAD_DONE, Field.REQUEST: upload.request})
    assert await receive_into(upload, tmp_path, lambda _text: None) == ["a: no", "b: the client sent nothing for it"]


async def test_a_client_too_old_to_send_a_file_is_refused():
    async with over_connection() as session:
        state, _ = await session.attach("old", SIZE, forwards=True)
        with set_app(state.app):
            await session.pymux.create_window(PANE_THAT_STAYS)
        pane = session.pymux.arrangement.get_active_window().panes[0]
        _out, errors = await answer_to(session, "receive x -d /", pane_id=pane.pane_id)
        assert "too old to send a file" in errors


async def test_the_request_ends_when_the_client_goes():
    async with over_connection() as session:
        state, _ = await session.attach("going", SIZE, forwards=True)
        connection = state.connection
        upload = connection.ask_for_files(["x"], "%1")
        await session.detach(state)
        with anyio.fail_after(10):
            [why] = await receive_into(upload, Path("/"), lambda _text: None)
        assert "went" in why


# ----------------------------------------------------------------------
# All the way: `pymux receive` in a pane, the server, the connection and
# the client.


async def test_pymux_receive_gets_a_file_from_the_client(tmp_path, home, config):
    source = home / "notes.txt"
    source.write_bytes(DATA * 30)
    into = tmp_path / "into"
    into.mkdir()
    said = tmp_path / "said"

    pymux = Pymux()
    pymux.serving.listen_on_socket(str(tmp_path / "pymux.sock"))
    connection_of: list = []
    client = AskedClient(config, send=lambda packet: connection_of[0]._process(packet))

    def read_packet(packet: dict) -> None:
        if packet[Field.CMD] == Packet.SEND_FILES:
            client._process(packet)

    async with over_connection(pymux=pymux, read_packet=read_packet) as session:
        state, _ = await session.attach("remote", SIZE, forwards=True)
        state.connection.sends_files = True
        connection_of.append(state.connection)
        async with anyio.create_task_group() as tasks:
            client._tasks = tasks
            with set_app(state.app):
                await pymux.create_window(
                    "/bin/sh -c %s"
                    % shlex.quote(
                        "%s -m pymux receive '~/notes.txt' -d %s >%s 2>&1; %s"
                        % (sys.executable, into, said, PANE_THAT_STAYS)
                    ),
                )

            await once(
                lambda: client._asking is not None,
                20.0,
                lambda: "the client was never asked; receive said %r" % (said.exists() and said.read_text(),),
            )
            client._take_input("y")

            target = into / "notes.txt"
            await once(
                lambda: said.exists() and "Saved" in said.read_text(),
                20.0,
                lambda: (
                    "receive never said it saved; it said %r, and the client sent %r"
                    % (said.exists() and said.read_text(), [packet[Field.CMD] for packet in client.sent])
                ),
            )
            assert target.read_bytes() == DATA * 30
            assert said.read_text() == "Saved %s.\n" % (target,)
            tasks.cancel_scope.cancel()


def test_the_shell_completion_of_receive_asks_the_person():
    from pymux.entry_points.run_pymux import _completion_parser, _person_paths

    modes = next(action for action in _completion_parser()._actions if isinstance(action, argparse._SubParsersAction))
    [paths] = [action for action in modes.choices["receive"]._actions if action.dest == "paths"]
    assert paths.completer is _person_paths


async def test_a_shell_in_a_pane_completes_the_persons_paths(tmp_path, home, config):
    "Inside the grant that `a` gave: the completer asks the server, which asks the client."
    (home / "notes").mkdir()
    (home / "notes" / "today.txt").write_bytes(b"x")
    (home / "notebook.pdf").write_bytes(b"x")
    said = tmp_path / "said"

    pymux = Pymux()
    pymux.serving.listen_on_socket(str(tmp_path / "pymux.sock"))
    connection_of: list = []
    client = AskedClient(config, send=lambda packet: connection_of[0]._process(packet))
    client._grant_until = client.clock.now + 60

    def read_packet(packet: dict) -> None:
        if packet[Field.CMD] == Packet.LIST_FILES:
            client._process(packet)

    complete = (
        "from pymux.entry_points.run_pymux import _person_paths\n"
        "for prefix in ('~/note', '~/notes/t'):\n"
        "    print([path for path in _person_paths(prefix) if path.startswith(prefix)])\n"
    )
    async with over_connection(pymux=pymux, read_packet=read_packet) as session:
        state, _ = await session.attach("remote", SIZE, forwards=True)
        state.connection.sends_files = True
        connection_of.append(state.connection)
        async with anyio.create_task_group() as tasks:
            client._tasks = tasks
            with set_app(state.app):
                await pymux.create_window(
                    "/bin/sh -c %s"
                    % shlex.quote(
                        "%s -c %s >%s 2>&1; %s" % (sys.executable, shlex.quote(complete), said, PANE_THAT_STAYS)
                    ),
                )
            await once(
                lambda: said.exists() and said.read_text().count("\n") >= 2,
                20.0,
                lambda: "the completer printed %r" % (said.exists() and said.read_text(),),
            )
            # The completer offers the whole directory, and the shell
            # keeps what starts with the word, as the print does here.
            assert said.read_text() == "['~/notebook.pdf', '~/notes/']\n['~/notes/today.txt']\n"
            tasks.cancel_scope.cancel()
