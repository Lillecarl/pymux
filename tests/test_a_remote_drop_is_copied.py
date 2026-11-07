"""
A drop of files from another machine: pymux pulls each file over kitty's
drag and drop protocol into a directory of its own, breadth first, and
pastes the paths there. The packets here are spelled as kitty's dnd.c
sends them: data in chunks with `m=1`, an empty end, `X=1` for a link
and `X=handle` for a directory. Lillecarl/pyterm#894.
"""

from __future__ import annotations

import base64
import os
from dataclasses import replace
from pathlib import Path

from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from pyte.sequences.kitty_dnd import DndPacket, DndType
from session import once, over_connection

from pymux.core.drop_files import MAX_COPY_BYTES, RemoteCopy, remove_old_drops
from pymux.core.main import Pymux

URIS = "file:///Users/carl/notes.txt\r\n# a comment\r\nhttps://x.org/\r\nfile:///Users/carl/My%20Dir/\r\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def answer(request: DndPacket, payload: bytes, kind: int = 0) -> list[DndPacket]:
    "kitty's response to `request`: its keys, `X` for the kind, the data in chunks, and the end."
    head = replace(request, pixel_x=kind)
    encoded = b64(payload)
    chunks = [encoded[i : i + 8] for i in range(0, len(encoded), 8)]
    packets = [replace(head, more=1, payload=chunk) for chunk in chunks[:-1]]
    if chunks:
        packets.append(replace(head, payload=chunks[-1]))
    return [*packets, head]


def serve(copy: RemoteCopy, files: dict[tuple[int, int, int], tuple[bytes, int] | str]) -> list[DndPacket]:
    "Answer every request of `copy` from `files`, by (x, y, Y). A str answers an error."
    sent = copy.start()
    asked = []
    while not copy.done:
        requests = [packet for packet in sent if packet.cell_x or packet.cell_y]
        assert requests, "the copy asks nothing and is not done: %r" % sent
        request = requests[-1]
        asked.append(request)
        found = files[(request.cell_x, request.cell_y, request.pixel_y)]
        if isinstance(found, str):
            sent = copy.took(replace(request, type=DndType.REQUEST_ERROR, payload=found))
            continue
        data, kind = found
        sent = []
        for packet in answer(request, data, kind):
            sent += copy.took(packet)
    return asked


def test_files_a_directory_and_a_link_are_copied(tmp_path: Path):
    copy = RemoteCopy(tmp_path / "drop", 3, URIS)
    asked = serve(
        copy,
        {
            (3, 1, 0): (b"hello\n" * 100, 0),
            (3, 3, 0): (b"a.txt\0sub\0link", 7),
            (1, 0, 7): (b"in a\n", 0),
            (2, 0, 7): (b"deep", 8),
            (3, 0, 7): (b"../a.txt", 1),
            (1, 0, 8): (b"", 0),
        },
    )
    root = tmp_path / "drop"
    assert (root / "notes.txt").read_bytes() == b"hello\n" * 100
    assert (root / "My Dir" / "a.txt").read_bytes() == b"in a\n"
    assert os.readlink(root / "My Dir" / "link") == "../a.txt"
    assert (root / "My Dir" / "sub" / "deep").read_bytes() == b""
    assert copy.pasted == [str(root / "notes.txt"), "https://x.org/", str(root / "My Dir")]
    # Breadth first: the directory's entries before its subdirectory's.
    assert [(p.cell_x, p.cell_y, p.pixel_y) for p in asked] == [
        (3, 1, 0),
        (3, 3, 0),
        (1, 0, 7),
        (2, 0, 7),
        (3, 0, 7),
        (1, 0, 8),
    ]


def test_a_directory_handle_is_closed_after_its_entries(tmp_path: Path):
    copy = RemoteCopy(tmp_path / "drop", 1, "file:///d/\n")
    sent = copy.start()
    for packet in answer(sent[-1], b"one", 5):
        sent = copy.took(packet)
    assert sent == [DndPacket(DndType.REQUEST, cell_x=1, pixel_y=5)]
    for packet in answer(sent[-1], b"1", 0):
        sent = copy.took(packet)
    assert sent == [DndPacket(DndType.REQUEST, pixel_y=5)]
    assert copy.done


def test_an_entry_that_does_not_come_is_left_out(tmp_path: Path):
    copy = RemoteCopy(tmp_path / "drop", 1, "file:///a\nfile:///b\n")
    serve(copy, {(1, 1, 0): "ENOENT:gone", (1, 2, 0): (b"b", 0)})
    assert copy.pasted == [str(tmp_path / "drop" / "b")]


def test_an_unsafe_name_is_skipped(tmp_path: Path):
    copy = RemoteCopy(tmp_path / "drop", 1, "file:///d/\n")
    serve(copy, {(1, 1, 0): (b"..\0ok", 4), (2, 0, 4): (b"fine", 0)})
    assert sorted(os.listdir(tmp_path / "drop" / "d")) == ["ok"]


def test_too_much_ends_the_copy_with_nothing_pasted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("pymux.core.drop_files.MAX_COPY_BYTES", 4)
    copy = RemoteCopy(tmp_path / "drop", 1, "file:///big\n")
    sent = copy.start()
    for packet in answer(sent[-1], b"0123456789", 0):
        copy.took(packet)
    assert copy.done and copy.pasted is None
    assert MAX_COPY_BYTES > 4


def test_two_entries_of_one_name_get_two_places(tmp_path: Path):
    copy = RemoteCopy(tmp_path / "drop", 1, "file:///a/x\nfile:///b/x\n")
    serve(copy, {(1, 1, 0): (b"1", 0), (1, 2, 0): (b"2", 0)})
    assert copy.pasted == [str(tmp_path / "drop" / "x"), str(tmp_path / "drop" / "x-2")]


def osc(param: str) -> str:
    return "\x1b]72;%s\x1b\\" % param


async def test_a_drop_from_another_machine_pastes_the_copy(tmp_path: Path):
    out: list[str] = []

    def read_packet(packet) -> None:
        if packet.get("cmd") == "out":
            out.append(packet.get("data", ""))

    def sent(text: str):
        return once(lambda: text in "".join(out), 10.0, lambda: "never sent %r: %r" % (text, "".join(out)[-300:]))

    pymux = Pymux(startup_command="sleep 30")
    pymux.drag_and_drop.drops = tmp_path
    async with over_connection(pymux=pymux, read_packet=read_packet) as session:
        state, _ = await session.attach("one", Size(rows=12, columns=60))
        state.client.connection._process({Field.CMD: Packet.DETECT})
        await sent(osc("t=q"))
        state.client.connection._process({"cmd": "in", "data": osc("t=q")})
        state.app.invalidate()
        await once(lambda: state.layout_manager.pane_write_positions, 10.0, "the pane was never drawn")
        ((pane, at),) = state.layout_manager.pane_write_positions.items()
        typed: list[str] = []
        pane.terminal.terminal_control.process.write_input = typed.append

        def say(text: str) -> None:
            state.client.connection._process({"cmd": "in", "data": text})

        say(osc("t=M:x=%d:y=%d:X=0:Y=0:o=1;text/uri-list" % (at.xpos, at.ypos)))
        await sent(osc("t=r:x=1"))
        say(osc("t=r:x=1:X=1;" + b64(b"file:///Users/carl/a%20b.txt\r\n")) + osc("t=r:x=1:X=1"))
        await sent(osc("t=r:x=1:y=1"))
        say(osc("t=r:x=1:y=1;" + b64(b"from the mac")) + osc("t=r:x=1:y=1"))
        await once(lambda: typed, 10.0, "nothing was pasted")
        (root,) = tmp_path.iterdir()
        assert typed == ["'%s'" % (root / "a b.txt")]
        assert (root / "a b.txt").read_bytes() == b"from the mac"
        await sent(osc("t=r:o=1"))


def old_and_new_drops(base: Path) -> tuple[Path, Path]:
    "A drop's copy from ten days ago, with a tree in it, and one from now."
    old, new = base / "20261001-120000-1", base / "20261103-120000-1"
    (old / "dir").mkdir(parents=True)
    (old / "dir" / "file").write_text("old")
    new.mkdir()
    (new / "file").write_text("new")
    ten_days_ago = os.stat(new).st_mtime - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))
    return old, new


def test_copies_older_than_the_days_kept_go(tmp_path: Path):
    "Lillecarl/pyterm#906."
    old, new = old_and_new_drops(tmp_path)
    assert remove_old_drops(tmp_path, 7) == [old]
    assert not old.exists()
    assert (new / "file").read_text() == "new"


def test_zero_days_keeps_every_copy(tmp_path: Path):
    old, new = old_and_new_drops(tmp_path)
    assert remove_old_drops(tmp_path, 0) == []
    assert old.exists() and new.exists()


async def test_the_server_removes_old_copies_at_its_start(tmp_path: Path):
    old, new = old_and_new_drops(tmp_path)
    pymux = Pymux(startup_command="sleep 30")
    pymux.drag_and_drop.drops = tmp_path
    async with over_connection(pymux=pymux):
        await once(lambda: not old.exists(), 10.0, "the old copy stays")
    assert new.exists()
