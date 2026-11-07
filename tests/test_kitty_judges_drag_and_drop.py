"""
kitty's own drag and drop code (dnd.c, in-process, through its test
hooks) against what pymux sends a terminal: kitty reads pymux's answers
as the spec means them, and a remote drop that kitty serves from real
files comes out whole in pymux's copy. Lillecarl/pyterm#894.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pyte.kitty_dnd import KittyDnd
from pyte.sequences.kitty_dnd import MACHINE_ID, DndPacket, DndType

from pymux.core.drop_files import RemoteCopy

#: GLFW's names for what kitty tells the system a drop does.
GLFW_DRAG_OPERATION_NONE = 0
GLFW_DRAG_OPERATION_COPY = 2

#: Not this machine's id, as kitty hashes it: kitty takes the client as remote.
ELSEWHERE = "1:" + "0" * 64

_PACKET = re.compile(rb"\x1b\]72;(.*?)\x1b\\", re.DOTALL)


def kitty_is_available() -> bool:
    path = os.environ.get("PYMUX_KITTY")
    if not path:
        return False
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import kitty.fast_data_types  # noqa: F401
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not kitty_is_available(), reason="the kitty python package is not there")


class Kitty:
    "One fake kitty window, its drop side driven by `send` and read by `said`."

    def __init__(self, window_id: int) -> None:
        from kitty.fast_data_types import Screen

        self.window_id = window_id
        self.screen = Screen(None, 24, 80, 0, 0, 0, window_id)
        self.written = bytearray()
        self.incoming = KittyDnd()

    def __call__(self, window_id: int, data: bytes) -> None:
        self.written += data

    def send(self, *packets: DndPacket) -> None:
        "What pymux writes to its terminal, to this one."
        data = memoryview("".join(str(packet) for packet in packets).encode())
        while data:
            written = self.screen.test_commit_write_buffer(data, self.screen.test_create_write_buffer())
            data = data[written:]
            self.screen.test_parse_written_data()

    def said(self) -> list[DndPacket]:
        "What kitty wrote back since the last look, read as pymux reads a terminal."
        data, self.written = bytes(self.written), bytearray()
        packets = (self.incoming.take(match.group(1).decode()) for match in _PACKET.finditer(data))
        return [packet for packet in packets if packet is not None]

    def probe(self, what: str):
        from kitty.fast_data_types import dnd_test_probe_state

        return dnd_test_probe_state(self.window_id, what)


@contextmanager
def kitty() -> Iterator[Kitty]:
    from kitty.fast_data_types import (
        dnd_set_test_write_func,
        dnd_test_cleanup_fake_window,
        dnd_test_create_fake_window,
        set_options,
    )
    from kitty.options.types import defaults

    set_options(defaults)
    os_window_id, window_id = dnd_test_create_fake_window()
    window = Kitty(window_id)
    dnd_set_test_write_func(window)
    try:
        yield window
    finally:
        dnd_set_test_write_func(None)
        dnd_test_cleanup_fake_window(os_window_id)


def drop(window: Kitty, mimes: list[str]) -> None:
    from kitty.fast_data_types import dnd_test_fake_drop_event

    dnd_test_fake_drop_event(window.window_id, True, mimes, 1, 1, 1)


def test_kitty_reads_pymux_taking_a_drop_and_ending_it():
    with kitty() as window:
        window.send(DndPacket(DndType.ACCEPT, payload="text/uri-list text/plain"))
        drop(window, ["text/plain", "text/uri-list"])
        (event,) = window.said()
        assert (event.type, event.payload.split()) == (DndType.DROP, ["text/plain", "text/uri-list"])

        window.send(DndPacket(DndType.MOVE, operation=1, payload="text/uri-list"))
        assert window.probe("drop_action") == GLFW_DRAG_OPERATION_COPY
        assert window.probe("drop_mimes").rstrip("\0") == "text/uri-list"

        window.send(DndPacket(DndType.REQUEST, operation=1))
        assert window.probe("last_drop_action") == GLFW_DRAG_OPERATION_COPY

        window.send(DndPacket(DndType.REQUEST))
        assert window.probe("last_drop_action") == GLFW_DRAG_OPERATION_NONE


def test_kitty_reads_the_machine_id_pymux_sends():
    with kitty() as window:
        window.send(DndPacket(DndType.ACCEPT), DndPacket(DndType.ACCEPT, cell_x=MACHINE_ID, payload=ELSEWHERE))
        assert window.probe("drop_is_remote_client")
        # A registration clears it, which is why pymux sends the id after.
        window.send(DndPacket(DndType.ACCEPT))
        assert not window.probe("drop_is_remote_client")


def test_a_remote_drop_kitty_serves_comes_out_whole(tmp_path: Path):
    from kitty.fast_data_types import dnd_test_fake_drop_data

    there = tmp_path / "there"
    (there / "My Dir" / "sub").mkdir(parents=True)
    (there / "notes.txt").write_bytes(bytes(range(256)) * 50)
    (there / "My Dir" / "a.txt").write_text("in a\n")
    (there / "My Dir" / "sub" / "deep").write_bytes(b"")
    (there / "My Dir" / "link").symlink_to("a.txt")
    uri_list = "file://%s\r\nhttps://x.org/\r\nfile://%s/\r\n" % (
        there / "notes.txt",
        str(there / "My Dir").replace(" ", "%20"),
    )

    with kitty() as window:
        window.send(
            DndPacket(DndType.ACCEPT, payload="text/uri-list"),
            DndPacket(DndType.ACCEPT, cell_x=MACHINE_ID, payload=ELSEWHERE),
        )
        drop(window, ["text/uri-list"])
        window.said()
        window.send(DndPacket(DndType.REQUEST, cell_x=1))
        dnd_test_fake_drop_data(window.window_id, "text/uri-list", uri_list.encode())
        answer = window.said()
        assert answer[0].pixel_x == 1, "kitty did not say the files are elsewhere"

        here = tmp_path / "here"
        copy = RemoteCopy(here, 1, uri_list)
        requests = copy.start()
        while not copy.done:
            assert requests, "the copy waits on nothing"
            window.send(*requests)
            requests = [request for packet in window.said() for request in copy.took(packet)]
        window.send(*requests)

    assert copy.pasted == [str(here / "notes.txt"), "https://x.org/", str(here / "My Dir")]
    assert (here / "notes.txt").read_bytes() == bytes(range(256)) * 50
    assert (here / "My Dir" / "a.txt").read_text() == "in a\n"
    assert (here / "My Dir" / "sub" / "deep").read_bytes() == b""
    assert os.readlink(here / "My Dir" / "link") == "a.txt"
