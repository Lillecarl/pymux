"""
A file that a program in a pane sends to the person: iTerm2's download
sequences, which client gets the file, and how the pymux client saves
it. Lillecarl/pymux#743.
"""

from __future__ import annotations

import base64
import io
import shlex
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import in_this_process, once, over_connection

from pymux import downloads
from pymux.client import downloads as client_downloads
from pymux.client.downloads import Downloads, Whole, download_directory, safe_name, save, save_whole
from pymux.client.terminal import TerminalClient
from pymux.downloads import (
    DEFAULT_NAME,
    DownloadReader,
    DownloadRefused,
    FinishedDownload,
    file_packets,
    file_sequence,
    multipart_sequences,
    route_of,
    terminal_saves_downloads,
)
from pymux.entry_points.run_pymux import Mode, parse_arguments
from pymux.enums import FileTransfer
from pymux.options import ALL_CLIENT_OPTIONS, SetOptionError
from pymux.osc import extension_key_of

SIZE = Size(rows=24, columns=80)

#: A pane that is still there when the test looks at it.
PANE_THAT_STAYS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

DATA = bytes(range(256)) * 40
NAME = "report.bin"


def b64(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return base64.b64encode(raw).decode("ascii")


def payload_of(sequence: str) -> str:
    'The part of an "OSC 1337" sequence after the code.'
    assert sequence.startswith("\x1b]1337;")
    assert sequence.endswith("\x1b\\")
    return sequence[len("\x1b]1337;") : -len("\x1b\\")]


def read_all(reader: DownloadReader, sequences) -> list[FinishedDownload]:
    "Each sequence through the reader the way a pane hands it over."
    finished = []
    for sequence in sequences:
        keyed = extension_key_of(payload_of(sequence))
        assert keyed is not None
        one = reader.read(*keyed)
        if one is not None:
            finished.append(one)
    return finished


# ----------------------------------------------------------------------
# Reading the sequences of a pane.


def test_a_single_file_is_read():
    [got] = read_all(DownloadReader(), [file_sequence(NAME, DATA)])
    assert got == FinishedDownload(NAME, DATA, multipart=False)


def test_a_file_with_no_inline_argument_is_a_download():
    "iTerm2's default for `inline` is 0: a download."
    reader = DownloadReader()
    got = reader.read("File", "name=%s:%s" % (b64(NAME), b64(DATA)))
    assert got == FinishedDownload(NAME, DATA, multipart=False)


def test_a_file_with_no_name_takes_the_default():
    got = DownloadReader().read("File", "size=3:%s" % (b64(b"abc"),))
    assert got is not None
    assert got.name == DEFAULT_NAME


def test_a_multipart_file_is_read():
    [got] = read_all(DownloadReader(), multipart_sequences(NAME, DATA, part_bytes=999))
    assert got == FinishedDownload(NAME, DATA, multipart=True)


def test_parts_need_not_be_whole_base64():
    "imgcat cuts the base64 text, not the bytes, so only the joined parts decode."
    text = b64(DATA)
    reader = DownloadReader()
    assert reader.read("MultipartFile", "name=%s" % (b64(NAME),)) is None
    for start in range(0, len(text), 7):
        assert reader.read("FilePart", text[start : start + 7]) is None
    assert reader.read("FileEnd", "") == FinishedDownload(NAME, DATA, multipart=True)


def test_an_inline_file_is_an_image_and_no_download():
    reader = DownloadReader()
    assert reader.read("File", "name=%s;inline=1:%s" % (b64(NAME), b64(DATA))) is None


def test_the_parts_of_an_inline_multipart_file_are_no_download():
    reader = DownloadReader()
    assert reader.read("MultipartFile", "inline=1") is None
    assert reader.read("FilePart", b64(DATA)) is None
    assert reader.read("FileEnd", "") is None


def test_a_part_with_no_file_open_is_dropped():
    reader = DownloadReader()
    assert reader.read("FilePart", b64(DATA)) is None
    assert reader.read("FileEnd", "") is None


def test_a_file_that_is_not_base64_is_refused():
    with pytest.raises(DownloadRefused):
        DownloadReader().read("File", "name=%s:not base64!" % (b64(NAME),))


def test_a_file_that_says_it_is_too_large_is_refused():
    with pytest.raises(DownloadRefused, match="larger than"):
        DownloadReader().read("File", "size=%d:%s" % (downloads.MAX_DOWNLOAD_BYTES + 1, b64(b"x")))


def test_a_single_file_too_large_is_refused(monkeypatch):
    monkeypatch.setattr(downloads, "MAX_DOWNLOAD_BYTES", 100)
    with pytest.raises(DownloadRefused, match="larger than"):
        DownloadReader().read("File", ":%s" % (b64(bytes(200)),))


def test_a_multipart_file_too_large_is_refused_and_its_rest_dropped(monkeypatch):
    monkeypatch.setattr(downloads, "MAX_DOWNLOAD_BYTES", 100)
    reader = DownloadReader()
    reader.read("MultipartFile", "name=%s" % (b64(NAME),))
    reader.read("FilePart", b64(bytes(60)))
    with pytest.raises(DownloadRefused, match="larger than"):
        reader.read("FilePart", b64(bytes(60)))
    # The rest of it reads as nothing, and the end finishes nothing.
    assert reader.read("FilePart", b64(bytes(60))) is None
    assert reader.read("FileEnd", "") is None
    # And the next file is read again.
    [got] = read_all(reader, multipart_sequences(NAME, b"small"))
    assert got.data == b"small"


# ----------------------------------------------------------------------
# Who saves it.


@pytest.mark.parametrize(
    ("chosen", "over_ssh", "terminal_saves", "route"),
    [
        (FileTransfer.AUTO, False, True, FileTransfer.PASSTHROUGH),
        # A local terminal that saves no download: the pymux client
        # writes to this machine's downloads directory. Lillecarl/pymux#746.
        (FileTransfer.AUTO, False, False, FileTransfer.PYMUX),
        (FileTransfer.AUTO, True, True, FileTransfer.PYMUX),
        (FileTransfer.PYMUX, False, True, FileTransfer.PYMUX),
        (FileTransfer.PYMUX, True, False, FileTransfer.PYMUX),
        (FileTransfer.PASSTHROUGH, False, False, FileTransfer.PASSTHROUGH),
        (FileTransfer.PASSTHROUGH, True, True, FileTransfer.PASSTHROUGH),
    ],
)
def test_the_route_of_a_download(chosen, over_ssh, terminal_saves, route):
    assert route_of(chosen, over_ssh, terminal_saves) is route


@pytest.mark.parametrize(
    ("version", "saves"),
    [("iTerm2 3.5.14", True), ("kitty(0.49.2)", False), ("ghostty 1.3.1", False), ("", False)],
)
def test_only_iterm2_saves_a_download(version, saves):
    assert terminal_saves_downloads(version) is saves


def test_the_option_takes_its_words_only():
    option = ALL_CLIENT_OPTIONS["file-transfer"]
    assert sorted(option.get_all_values(None)) == ["auto", "passthrough", "pymux"]


async def test_the_option_is_one_clients():
    async with in_this_process() as session:
        a, _ = await session.attach("a", SIZE)
        b, _ = await session.attach("b", SIZE)
        ALL_CLIENT_OPTIONS["file-transfer"].set_value(session.pymux, "pymux", target=a)
        assert a.file_transfer is FileTransfer.PYMUX
        assert b.file_transfer is FileTransfer.AUTO
        with pytest.raises(SetOptionError):
            ALL_CLIENT_OPTIONS["file-transfer"].set_value(session.pymux, "sideways", target=a)


class Collected:
    "What the server gave one stub client: escapes for its terminal, and packets."

    def __init__(self, state) -> None:
        self.sequences: list[str] = []
        self.packets: list[dict] = []
        state.connection.forward_osc = self.sequences.append
        state.connection._send_packet = self.packets.append

    def files(self) -> list[dict]:
        return [packet for packet in self.packets if packet[Field.CMD] == Packet.FILE]


async def a_pane(pymux, state):
    with set_app(state.app):
        await pymux.create_window(PANE_THAT_STAYS)
    return pymux.arrangement.get_active_window().panes[0]


def send(pymux, pane, sequences) -> None:
    "The sequences as the pane's screen hands them over."
    for sequence in sequences:
        pymux.forward_osc(pane, "1337", payload_of(sequence))


def saved(packets: list[dict]) -> bytes:
    return b"".join(base64.b64decode(packet[Field.DATA]) for packet in packets)


def on_iterm2(state) -> None:
    "The client's terminal answered XTVERSION as iTerm2, which saves a download."
    state.connection.graphics.terminal_version = "iTerm2 3.5.14"


async def test_a_local_client_gets_the_sequence():
    async with in_this_process() as session:
        state, _ = await session.attach("local", SIZE)
        on_iterm2(state)
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        sent = list(multipart_sequences(NAME, DATA))
        send(session.pymux, pane, sent)

        assert got.sequences == sent
        assert got.files() == []


async def test_a_single_file_passes_through_as_one():
    async with in_this_process() as session:
        state, _ = await session.attach("local", SIZE)
        on_iterm2(state)
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, [file_sequence(NAME, DATA)])

        assert got.sequences == [file_sequence(NAME, DATA)]


async def test_a_remote_client_gets_packets():
    async with in_this_process() as session:
        state, _ = await session.attach("remote", SIZE)
        state.connection.can_forward = True
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, multipart_sequences(NAME, DATA))

        assert got.sequences == []
        files = got.files()
        assert files[-1][Field.LAST] is True
        assert {packet[Field.NAME] for packet in files} == {NAME}
        assert saved(files) == DATA


@pytest.mark.parametrize(
    ("chosen", "remote", "packets"),
    [
        ("pymux", False, True),
        ("passthrough", True, False),
    ],
)
async def test_the_option_wins_over_auto(chosen, remote, packets):
    async with in_this_process() as session:
        state, _ = await session.attach("one", SIZE)
        on_iterm2(state)
        state.connection.can_forward = remote
        ALL_CLIENT_OPTIONS["file-transfer"].set_value(session.pymux, chosen, target=state)
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, [file_sequence(NAME, DATA)])

        assert bool(got.files()) is packets
        assert bool(got.sequences) is not packets


async def test_a_local_terminal_that_saves_no_download_gets_the_file_from_pymux():
    "auto: the pymux client writes to this machine's downloads directory. Lillecarl/pymux#746."
    async with in_this_process() as session:
        state, _ = await session.attach("local", SIZE)
        state.connection.graphics.terminal_version = "kitty(0.49.2)"
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, [file_sequence(NAME, DATA)])

        assert got.sequences == []
        assert saved(got.files()) == DATA


async def test_passthrough_to_a_terminal_that_saves_no_download_is_refused():
    "It would draw nothing, or the base64. The person is told why. Lillecarl/pymux#746."
    async with in_this_process() as session:
        state, _ = await session.attach("local", SIZE)
        ALL_CLIENT_OPTIONS["file-transfer"].set_value(session.pymux, "passthrough", target=state)
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, [file_sequence(NAME, DATA)])

        assert got.sequences == []
        assert got.files() == []
        assert "saves no iTerm2 download" in state.message


async def test_a_client_too_old_to_save_a_file_is_told_so():
    "It drops `file` packets unread, and the person never learned. Lillecarl/pymux#745."
    async with in_this_process() as session:
        state, _ = await session.attach("old", SIZE)
        state.connection.can_forward = True
        state.connection.saves_files = False
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, [file_sequence(NAME, DATA)])

        assert got.files() == []
        assert "too old to save one" in state.message


async def test_only_the_client_used_last_gets_it():
    async with in_this_process() as session:
        pymux = session.pymux
        a, _ = await session.attach("a", SIZE)
        b, _ = await session.attach("b", SIZE)
        on_iterm2(a)
        on_iterm2(b)
        got_a, got_b = Collected(a), Collected(b)
        pane = await a_pane(pymux, a)

        pymux.client_was_used(a)
        send(pymux, pane, [file_sequence(NAME, DATA)])

        assert got_a.sequences == [file_sequence(NAME, DATA)]
        assert got_b.sequences == []


async def test_a_download_nobody_is_attached_for_is_dropped(caplog):
    async with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("gone", SIZE)
        got = Collected(state)
        pane = await a_pane(pymux, state)
        await session.detach(state)

        send(pymux, pane, [file_sequence(NAME, DATA)])

        assert got.sequences == []
        assert got.files() == []
        assert "nobody is attached" in caplog.text


async def test_a_refused_download_tells_the_client(monkeypatch):
    monkeypatch.setattr(downloads, "MAX_DOWNLOAD_BYTES", 100)
    async with in_this_process() as session:
        state, _ = await session.attach("one", SIZE)
        got = Collected(state)
        pane = await a_pane(session.pymux, state)

        send(session.pymux, pane, [file_sequence(NAME, bytes(200))])

        assert got.sequences == []
        assert "larger than" in state.message


def test_a_large_file_goes_in_several_packets():
    data = bytes(downloads.PACKET_BYTES * 2 + 5)
    packets = list(file_packets(FinishedDownload(NAME, data, multipart=False)))
    assert len(packets) == 3
    assert [packet[Field.LAST] for packet in packets] == [False, False, True]
    assert len({packet[Field.TRANSFER] for packet in packets}) == 1
    assert saved(packets) == data


def test_an_empty_file_is_one_packet():
    [packet] = file_packets(FinishedDownload(NAME, b"", multipart=False))
    assert packet[Field.LAST] is True
    assert packet[Field.DATA] == ""


# ----------------------------------------------------------------------
# The pymux client saves it.


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("..\\..\\windows\\system.ini", "system.ini"),
        ("dir/", "dir"),
        ("/", DEFAULT_NAME),
        ("..", DEFAULT_NAME),
        (".", DEFAULT_NAME),
        ("", DEFAULT_NAME),
        ("evil\x1b]0;x\x07.txt", "evil]0;x.txt"),
        ("rapport för dig.txt", "rapport för dig.txt"),
    ],
)
def test_a_name_has_no_path_in_it(name, safe):
    assert safe_name(name) == safe


def test_a_long_name_is_cut_to_what_a_file_system_takes():
    assert len(safe_name("å" * 300).encode("utf-8")) <= client_downloads.MAX_NAME_BYTES


def test_a_save_never_overwrites(tmp_path):
    (tmp_path / NAME).write_bytes(b"there first")
    first = save(tmp_path, NAME, b"one")
    second = save(tmp_path, NAME, b"two")
    assert (tmp_path / NAME).read_bytes() == b"there first"
    assert first == tmp_path / "report (1).bin"
    assert second == tmp_path / "report (2).bin"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_a_save_stays_in_its_directory(tmp_path):
    inside = tmp_path / "downloads"
    inside.mkdir()
    path = save(inside, "../outside.txt", b"x")
    assert path == inside / "outside.txt"
    assert not (tmp_path / "outside.txt").exists()


def test_the_download_directory(tmp_path):
    home = tmp_path / "home"
    current = tmp_path / "here"
    named = tmp_path / "named"
    for one in (home, current, named):
        one.mkdir()

    # Nothing there: the current directory.
    assert download_directory({}, home, current) == current
    # ~/Downloads, once it is there.
    (home / "Downloads").mkdir()
    assert download_directory({}, home, current) == home / "Downloads"
    # The variable wins, in the form user-dirs.dirs writes.
    (home / "Fetched").mkdir()
    assert download_directory({"XDG_DOWNLOAD_DIR": "$HOME/Fetched"}, home, current) == home / "Fetched"
    assert download_directory({"XDG_DOWNLOAD_DIR": str(named)}, home, current) == named
    # A variable that names no directory is skipped.
    assert download_directory({"XDG_DOWNLOAD_DIR": str(tmp_path / "gone")}, home, current) == home / "Downloads"


def test_the_client_saves_the_parts_as_one_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    data = bytes(downloads.PACKET_BYTES + 10)
    receiving = Downloads()
    said = [receiving.receive(packet) for packet in file_packets(FinishedDownload(NAME, data, multipart=True))]
    assert said[:-1] == [None]
    whole = said[-1]
    assert isinstance(whole, Whole)
    assert save_whole(whole) == "Saved %s." % (tmp_path / NAME,)
    assert (tmp_path / NAME).read_bytes() == data


def test_the_client_bounds_a_file_too(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(client_downloads, "MAX_DOWNLOAD_BYTES", 10)
    receiving = Downloads()
    packets = list(file_packets(FinishedDownload(NAME, bytes(downloads.PACKET_BYTES + 10), multipart=True)))
    assert "larger than" in (receiving.receive(packets[0]) or "")
    assert receiving.receive(packets[1]) is None
    assert list(tmp_path.iterdir()) == []


class AClient(TerminalClient):
    "A terminal client that keeps what it would send to the server."

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    def _send_packet(self, data) -> None:
        self.sent.append(data)


def test_the_client_says_where_the_file_went(tmp_path, monkeypatch):
    "The socket client has no loop, so a thread writes the file and answers."
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    client = AClient()
    for packet in file_packets(FinishedDownload(NAME, DATA, multipart=False)):
        client._process(packet)
    deadline = time.monotonic() + 10
    while not client.sent:
        assert time.monotonic() < deadline, "the client never answered"
        time.sleep(0.01)
    assert client.sent == [{Field.CMD: Packet.FILE_SAVED, Field.MESSAGE: "Saved %s." % (tmp_path / NAME,)}]
    assert (tmp_path / NAME).read_bytes() == DATA


async def test_a_client_on_a_loop_draws_on_while_a_file_is_written(tmp_path, monkeypatch):
    "A write of 64 MiB on the loop stopped drawing and typing. Lillecarl/pymux#747."
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    written = threading.Event()
    real = client_downloads.save

    def slow_save(*arguments):
        written.wait(10)
        return real(*arguments)

    monkeypatch.setattr(client_downloads, "save", slow_save)
    client = AClient()
    async with anyio.create_task_group() as tasks:
        client._tasks = tasks
        for packet in file_packets(FinishedDownload(NAME, DATA, multipart=False)):
            client._process(packet)
        # The save is still held, and the loop runs on.
        await anyio.sleep(0.05)
        assert client.sent == []
        written.set()
        with anyio.fail_after(10):
            while not client.sent:
                await anyio.sleep(0.01)
    assert client.sent == [{Field.CMD: Packet.FILE_SAVED, Field.MESSAGE: "Saved %s." % (tmp_path / NAME,)}]


# ----------------------------------------------------------------------
# All the way: a program in a pane, the server, the connection and the
# client.


async def test_pymux_send_file_reaches_the_download_directory(tmp_path, monkeypatch):
    """
    `pymux send-file` in a pane writes the multipart sequence to the
    pane; the server reads it from the pane's screen and sends it to the
    client that came over SSH, which saves it.
    """
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(downloads_dir))
    source = tmp_path / "out" / "result.tar"
    source.parent.mkdir()
    source.write_bytes(DATA * 30)

    client = AClient()
    said = tmp_path / "stderr"
    files: list[dict] = []

    def read_packet(packet: dict) -> None:
        # Only the files: the rest is frames for a terminal there is not.
        if packet[Field.CMD] == Packet.FILE:
            files.append(packet)
            client._process(packet)

    def complaint() -> str:
        stderr = said.read_text() if said.exists() else "(no stderr)"
        parts = [(packet[Field.NAME], len(packet[Field.DATA]), packet[Field.LAST]) for packet in files]
        return "the file never arrived: file packets %r; the client said %r; %s holds %r; send-file said %r" % (
            parts,
            client.sent,
            downloads_dir,
            sorted(path.name for path in downloads_dir.iterdir()),
            stderr,
        )

    async with over_connection(read_packet=read_packet) as session:
        pymux = session.pymux
        state, _ = await session.attach("remote", SIZE, forwards=True)
        with set_app(state.app):
            await pymux.create_window(
                # A pane runs its command without a shell, so the
                # redirection and the second command need one.
                "/bin/sh -c %s"
                % shlex.quote("%s -m pymux send-file %s 2>%s; %s" % (sys.executable, source, said, PANE_THAT_STAYS)),
            )

        target = downloads_dir / "result.tar"
        await once(lambda: target.exists() and client.sent, 20.0, complaint)
        assert target.read_bytes() == DATA * 30

        # And the server shows the person where it went.
        [answer] = client.sent
        state.connection._process(answer)
        assert state.message == "Saved %s." % (target,)


def test_send_file_writes_the_multipart_sequence(tmp_path):
    source = tmp_path / NAME
    source.write_bytes(DATA)
    out, err = io.BytesIO(), io.StringIO()
    assert downloads.send_files([str(source)], out, err) == 0
    written = out.getvalue().decode("ascii")
    assert written == "".join(multipart_sequences(NAME, DATA))


def test_send_file_says_what_it_could_not_send(tmp_path):
    out, err = io.BytesIO(), io.StringIO()
    assert downloads.send_files([str(tmp_path / "missing")], out, err) == 1
    assert "missing" in err.getvalue()
    assert out.getvalue() == b""
    assert downloads.send_files([], out, err) == 2


def test_send_file_is_a_mode_and_not_a_command():
    a, mode, command = parse_arguments(["send-file", "a.txt", "b.txt"])
    assert mode is Mode.SEND_FILE
    assert command is None
    assert a.send_paths == ["a.txt", "b.txt"]


def test_send_is_still_send_keys():
    "tmux's `send` names `send-keys`, and keeps doing so."
    _a, mode, command = parse_arguments(["send", "-t", "1", "ls"])
    assert mode is None
    assert command == "send -t 1 ls"


def test_no_file_is_written_outside(tmp_path: Path):
    "`..` in a name the server forwards still lands in the directory."
    receiving = Downloads()
    packets = file_packets(FinishedDownload("../../escape", b"x", multipart=False))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
        for packet in packets:
            arrived = receiving.receive(packet)
        assert isinstance(arrived, Whole)
        save_whole(arrived)
    assert (tmp_path / "escape").read_bytes() == b"x"
