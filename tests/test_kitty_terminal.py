"""
The pymux client as the terminal of kitty's file transfer, for a
terminal that is not kitty. Lillecarl/pymux#912.
"""

from __future__ import annotations

import os
import shlex
import sys
import zlib
from pathlib import Path

import anyio
import pytest
from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from pyte.sequences.file_transfer import Action, Compression, FileType, TransferCommand, TransmissionType
from session import once, over_connection
from test_a_pane_asks_for_a_file import AskedClient, config, home  # noqa: F401

from pymux.client.kitty_transfer import REFUSED, Question, Transfers, Unasked
from pymux.core.main import Pymux
from pymux.downloads import MAX_DOWNLOAD_BYTES

SIZE = Size(rows=24, columns=80)
DATA = bytes(range(256)) * 40


def _transfers(home: Path, may_read: bool = False) -> Transfers:
    return Transfers(home, lambda paths: may_read)


def _said(transfers: Transfers, *commands: TransferCommand) -> list:
    out = []
    for command in commands:
        out.extend(transfers.take(command.param(), "%3"))
    return out


def _statuses(effects: list) -> list[str]:
    return [effect.status for effect in effects if isinstance(effect, TransferCommand)]


def _data(effects: list, file_id: str) -> bytes:
    return b"".join(
        effect.data
        for effect in effects
        if isinstance(effect, TransferCommand) and effect.file_id == file_id and effect.action is not Action.STATUS
    )


def _pumped(transfers: Transfers) -> list:
    out = []
    while transfers.has_data:
        out.extend(transfers.pump())
    return out


# ----------------------------------------------------------------------
# The program sends, and the files land here.


def _accepted_writing(transfers: Transfers) -> None:
    [question] = _said(transfers, TransferCommand(Action.SEND, id="w"))
    assert question == Question("w", "%3", reads=False)
    assert _statuses(transfers.answered("w", True)) == ["OK"]


def test_a_sent_file_lands_where_the_program_says(home):
    transfers = _transfers(home)
    _accepted_writing(transfers)
    started = _said(transfers, TransferCommand(Action.FILE, id="w", file_id="f", name="~/in/a.bin", permissions=0o640))
    [status] = started
    assert (status.status, status.name, status.size) == ("STARTED", str(home / "in" / "a.bin"), -1)

    effects = _said(
        transfers,
        TransferCommand(Action.DATA, id="w", file_id="f", data=DATA[:5000]),
        TransferCommand(Action.END_DATA, id="w", file_id="f", data=DATA[5000:]),
        TransferCommand(Action.FINISH, id="w"),
    )
    assert _statuses(effects) == ["PROGRESS", "OK"]
    assert (home / "in" / "a.bin").read_bytes() == DATA
    assert (home / "in" / "a.bin").stat().st_mode & 0o777 == 0o640
    assert transfers.writing == {}


def test_a_sent_file_writes_over_one_that_is_there(home):
    "As kitty does: Carl chose it, so a transfer run again brings a file up to date."
    (home / "a.bin").write_bytes(b"old")
    transfers = _transfers(home)
    _accepted_writing(transfers)
    [started] = _said(transfers, TransferCommand(Action.FILE, id="w", file_id="f", name="a.bin"))
    assert started.size == 3
    _said(transfers, TransferCommand(Action.END_DATA, id="w", file_id="f", data=b"new"))
    assert (home / "a.bin").read_bytes() == b"new"


def test_a_sent_file_writes_through_no_symlink(home, tmp_path):
    "A symlink at the destination is replaced, so the write never lands where it points."
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"keep")
    (home / "a.bin").symlink_to(elsewhere)
    transfers = _transfers(home)
    _accepted_writing(transfers)
    _said(
        transfers,
        TransferCommand(Action.FILE, id="w", file_id="f", name="a.bin"),
        TransferCommand(Action.END_DATA, id="w", file_id="f", data=b"new"),
    )
    assert elsewhere.read_bytes() == b"keep"
    assert not (home / "a.bin").is_symlink()


def test_a_compressed_file_and_a_directory_and_a_link(home):
    transfers = _transfers(home)
    _accepted_writing(transfers)
    squeezed = zlib.compress(DATA)
    effects = _said(
        transfers,
        TransferCommand(Action.FILE, id="w", file_id="d", name="dir", file_type=FileType.DIRECTORY),
        TransferCommand(Action.FILE, id="w", file_id="f", name="dir/a.bin", compression=Compression.ZLIB),
        TransferCommand(Action.FILE, id="w", file_id="s", name="dir/link", file_type=FileType.SYMLINK),
        TransferCommand(Action.DATA, id="w", file_id="f", data=squeezed[:100]),
        TransferCommand(Action.END_DATA, id="w", file_id="f", data=squeezed[100:]),
        TransferCommand(Action.END_DATA, id="w", file_id="s", data=b"fid:f"),
    )
    assert "ERR" not in "".join(_statuses(effects))
    assert (home / "dir" / "a.bin").read_bytes() == DATA
    assert os.readlink(home / "dir" / "link") == "a.bin"


def test_no_writes_nothing_and_says_so(home):
    transfers = _transfers(home)
    _said(transfers, TransferCommand(Action.SEND, id="w"))
    assert _statuses(transfers.answered("w", False)) == ["EPERM:" + REFUSED]
    assert _said(transfers, TransferCommand(Action.FILE, id="w", file_id="f", name="a.bin")) == []
    assert not (home / "a.bin").exists()


def test_a_command_before_the_answer_drops_the_session_and_its_question(home):
    "As kitty drops it."
    transfers = _transfers(home)
    _said(transfers, TransferCommand(Action.SEND, id="w"))
    effects = _said(transfers, TransferCommand(Action.FILE, id="w", file_id="f", name="a.bin"))
    assert effects == [Unasked("w")]
    assert transfers.answered("w", True) == []


def test_a_cancel_is_acknowledged(home):
    transfers = _transfers(home)
    _accepted_writing(transfers)
    assert _statuses(_said(transfers, TransferCommand(Action.CANCEL, id="w"))) == ["CANCELED"]


def test_a_quiet_session_says_nothing_but_its_errors(home):
    transfers = _transfers(home)
    _said(transfers, TransferCommand(Action.SEND, id="w", quiet=1))
    assert transfers.answered("w", True) == []
    effects = _said(
        transfers,
        TransferCommand(Action.FILE, id="w", file_id="f", name="a.bin"),
        TransferCommand(Action.END_DATA, id="w", file_id="f", data=b"x"),
        TransferCommand(Action.END_DATA, id="w", file_id="nobody", data=b"x"),
    )
    assert _statuses(effects) == ["EINVAL:Cannot write to a file without first starting it"]


# ----------------------------------------------------------------------
# The program reads files here.


def _accepted_reading(transfers: Transfers, *names: str) -> list:
    effects = _said(transfers, TransferCommand(Action.RECEIVE, id="r", size=len(names)))
    effects += _said(
        transfers, *(TransferCommand(Action.FILE, id="r", file_id="q%d" % n, name=name) for n, name in enumerate(names))
    )
    return effects


def test_a_read_is_asked_about_by_name_then_listed(home):
    (home / "a.bin").write_bytes(DATA)
    transfers = _transfers(home)
    [question] = _accepted_reading(transfers, "~/a.bin")
    assert question == Question("r", "%3", reads=True, paths=("~/a.bin",))

    effects = transfers.answered("r", True)
    [permitted, listed, done] = effects
    assert permitted.status == "OK"
    assert (listed.action, listed.name, listed.size, listed.file_id) == (
        Action.FILE,
        str(home / "a.bin"),
        len(DATA),
        "q0",
    )
    assert (done.status, done.name) == ("OK", str(home))

    _said(transfers, TransferCommand(Action.FILE, id="r", file_id=listed.status, name=listed.name))
    sent = _pumped(transfers)
    assert _data(sent, listed.status) == DATA
    assert sent[-1].action is Action.END_DATA
    assert all(len(command.data) <= 4096 for command in sent)


def test_a_read_comes_compressed_when_asked(home):
    (home / "a.bin").write_bytes(DATA)
    transfers = _transfers(home, may_read=True)
    effects = _accepted_reading(transfers, "a.bin")
    [listed] = [effect for effect in effects if isinstance(effect, TransferCommand) and effect.action is Action.FILE]
    _said(
        transfers,
        TransferCommand(Action.FILE, id="r", file_id=listed.status, name=listed.name, compression=Compression.ZLIB),
    )
    assert zlib.decompress(_data(_pumped(transfers), listed.status)) == DATA


def test_a_grant_or_upload_paths_reads_without_a_question(home):
    (home / "a.bin").write_bytes(DATA)
    effects = _accepted_reading(_transfers(home, may_read=True), "a.bin")
    assert not any(isinstance(effect, Question) for effect in effects)
    assert _statuses(effects)[0] == "OK"


def test_a_directory_lists_what_it_holds(home):
    (home / "dir").mkdir()
    (home / "dir" / "a.bin").write_bytes(DATA)
    os.link(home / "dir" / "a.bin", home / "dir" / "b.bin")
    (home / "dir" / "link").symlink_to("a.bin")
    effects = _accepted_reading(_transfers(home, may_read=True), "dir")
    listed = {
        Path(effect.name).name: effect
        for effect in effects
        if isinstance(effect, TransferCommand) and effect.action is Action.FILE
    }
    assert set(listed) == {"dir", "a.bin", "b.bin", "link"}
    assert listed["a.bin"].parent == listed["dir"].status
    first, second = sorted(["a.bin", "b.bin"], key=lambda name: listed[name].file_type is FileType.LINK)
    assert listed[second].file_type is FileType.LINK
    assert listed[second].data == listed[first].status.encode()
    assert listed["link"].file_type is FileType.SYMLINK


def test_only_a_listed_file_is_read(home, tmp_path):
    "A program that was granted one file reads that one, and not another it names later."
    (home / "a.bin").write_bytes(DATA)
    secret = tmp_path / "secret"
    secret.write_bytes(b"no")
    transfers = _transfers(home, may_read=True)
    _accepted_reading(transfers, "a.bin")
    effects = _said(transfers, TransferCommand(Action.FILE, id="r", file_id="x", name=str(secret)))
    assert _statuses(effects) == ["EPERM:%s is not in the listing" % (secret,)]
    assert transfers.reading == {}


def test_a_delta_is_refused(home):
    (home / "a.bin").write_bytes(DATA)
    transfers = _transfers(home, may_read=True)
    _accepted_reading(transfers, "a.bin")
    effects = _said(
        transfers,
        TransferCommand(
            Action.FILE, id="r", file_id="0", name=str(home / "a.bin"), transmission_type=TransmissionType.RSYNC
        ),
    )
    [refused] = _statuses(effects)
    assert refused.startswith("EINVAL:pymux sends no rsync delta")


def test_a_file_past_the_bound_is_not_read(home, monkeypatch):
    from pymux.client import kitty_transfer

    monkeypatch.setattr(kitty_transfer, "MAX_DOWNLOAD_BYTES", 100)
    (home / "a.bin").write_bytes(DATA)
    transfers = _transfers(home, may_read=True)
    _accepted_reading(transfers, "a.bin")
    effects = _said(transfers, TransferCommand(Action.FILE, id="r", file_id="0", name=str(home / "a.bin")))
    [refused] = _statuses(effects)
    assert refused.startswith("EFBIG:")
    assert MAX_DOWNLOAD_BYTES > 100


def test_a_missing_path_is_said_for_its_spec(home):
    effects = _accepted_reading(_transfers(home, may_read=True), "nothing")
    assert _statuses(effects) == ["OK", "ENOENT:Failed to read spec", "OK"]


# ----------------------------------------------------------------------
# All the way: `kitten transfer` in a pane, the server, and the client.

KITTEN = os.environ.get("PYMUX_KITTEN", "")
PANE_THAT_STAYS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


async def _through_the_client(tmp_path: Path, config: Path, command: str, answer: str):
    "Run `command` in a pane whose client answers kitty's file transfer, answer its question, and wait."
    said = tmp_path / "said"
    pymux = Pymux()
    pymux.serving.listen_on_socket(str(tmp_path / "pymux.sock"))
    connection_of: list = []
    client = AskedClient(config, send=lambda packet: connection_of[0]._process(packet))

    def read_packet(packet: dict) -> None:
        if packet[Field.CMD] == Packet.KITTY_TRANSFER:
            client._process(packet)

    async with over_connection(pymux=pymux, read_packet=read_packet) as session:
        state, _ = await session.attach("remote", SIZE, forwards=True)
        state.connection.answers_transfers = True
        connection_of.append(state.connection)
        async with anyio.create_task_group() as tasks:
            client.tasks = tasks
            with set_app(state.app):
                await pymux.panes.create_window(
                    "/bin/sh -c %s"
                    % shlex.quote("%s >%s 2>&1; echo status=$? >>%s; %s" % (command, said, said, PANE_THAT_STAYS))
                )
            await once(
                lambda: client.sending_files.asking is not None,
                20.0,
                lambda: "the client was never asked; the kitten said %r" % (said.exists() and said.read_text(),),
            )
            client._take_input(answer)
            await once(
                lambda: said.exists() and "status=" in said.read_text(),
                30.0,
                lambda: "the kitten never ended; it said %r" % (said.exists() and said.read_text(),),
            )
            tasks.cancel_scope.cancel()
    return client, said.read_text()


@pytest.mark.skipif(not KITTEN, reason="needs the kitten: PYMUX_KITTEN")
async def test_kitten_transfer_sends_a_file_through_the_pymux_client(tmp_path, home, config):
    source = tmp_path / "source.bin"
    source.write_bytes(DATA * 30)
    target = home / "target.bin"
    client, said = await _through_the_client(tmp_path, config, "%s transfer %s %s" % (KITTEN, source, target), "y")
    assert "status=0" in said, said
    assert target.read_bytes() == DATA * 30
    assert "wants to write files here" in b"".join(client.drawn).decode()


@pytest.mark.skipif(not KITTEN, reason="needs the kitten: PYMUX_KITTEN")
async def test_kitten_transfer_reads_a_file_through_the_pymux_client(tmp_path, home, config):
    (home / "notes.txt").write_bytes(DATA * 30)
    target = tmp_path / "got.txt"
    client, said = await _through_the_client(
        tmp_path, config, "%s transfer --direction=upload %s %s" % (KITTEN, home / "notes.txt", target), "y"
    )
    assert "status=0" in said, said
    assert target.read_bytes() == DATA * 30
    assert "~/notes.txt" in b"".join(client.drawn).decode()


@pytest.mark.skipif(not KITTEN, reason="needs the kitten: PYMUX_KITTEN")
async def test_kitten_transfer_hears_no(tmp_path, home, config):
    source = tmp_path / "source.bin"
    source.write_bytes(DATA)
    target = home / "target.bin"
    _client, said = await _through_the_client(tmp_path, config, "%s transfer %s %s" % (KITTEN, source, target), "n")
    assert "status=0" not in said
    assert not target.exists()
