"""
A window pane's frame as kitty commands, encoded where the terminal is.
Lillecarl/pymux#634.
"""

from __future__ import annotations

import base64
import re
import zlib
from multiprocessing import shared_memory

import pytest

from pymux.kitty_frames import SHM_NAME_MAX, SHM_PROBE_ID, Base64Carrier, ShmCarrier, frame_commands, shm_probe

#: One graphics command: its keys and its payload.
COMMAND = re.compile(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\")

RED = bytes([0xFF, 0, 0])


def parsed(text: str) -> list[tuple[dict[str, str], str]]:
    "Each command of `text` with its chunks joined: its keys and its whole payload."
    joined: list[tuple[dict[str, str], str]] = []
    for keys, payload in COMMAND.findall(text):
        fields = dict(pair.split("=", 1) for pair in keys.split(","))
        if joined and set(fields) == {"m"}:
            joined[-1] = (joined[-1][0], joined[-1][1] + payload)
        else:
            joined.append((fields, payload))
    return joined


@pytest.fixture
def shm():
    carrier = ShmCarrier()
    yield carrier
    carrier.close()


def read_shm(payload: str, size: int) -> bytes:
    name = base64.b64decode(payload).decode()
    assert name.startswith("/")
    memory = shared_memory.SharedMemory(name[1:], track=False)
    try:
        return bytes(memory.buf[:size])
    finally:
        memory.close()
        memory.unlink()


def test_a_whole_frame_in_base64_is_the_pixels_compressed():
    pixels = RED * (40 * 30)
    text = frame_commands(7, True, 40, 30, [(0, 0, 40, 30, pixels)], Base64Carrier())
    ((keys, payload),) = parsed(text)
    assert keys == {"a": "t", "i": "7", "s": "40", "v": "30", "q": "2", "f": "24", "o": "z", "m": "0"}
    assert zlib.decompress(base64.b64decode(payload)) == pixels


def test_a_large_frame_goes_in_chunks_kitty_takes():
    pixels = bytes(range(256)) * 3000
    text = frame_commands(7, True, 1000, 256, [(0, 0, 1000, 256, pixels)], Base64Carrier())
    chunks = COMMAND.findall(text)
    assert len(chunks) > 1
    assert all(len(payload) <= 4096 for _keys, payload in chunks)
    assert [keys.endswith("m=1") or keys == "m=1" for keys, _ in chunks[:-1]] == [True] * (len(chunks) - 1)
    assert chunks[-1][0].endswith("m=0")
    ((_keys, payload),) = parsed(text)
    assert zlib.decompress(base64.b64decode(payload)) == pixels


def test_damage_goes_as_edits_of_the_root_frame():
    "The keys `pysmithay/src/kitty.rs` writes, so a terminal sees one protocol."
    rects = [(10, 5, 4, 4, RED * 16), (0, 0, 1, 1, RED)]
    text = frame_commands(7, False, 64, 32, rects, Base64Carrier())
    got = parsed(text)
    assert [keys for keys, _ in got] == [
        {"a": "f", "i": "7", "r": "1", "x": "10", "y": "5", "s": "4", "v": "4", "X": "1", "q": "2", "f": "24", "o": "z", "m": "0"},
        {"a": "f", "i": "7", "r": "1", "x": "0", "y": "0", "s": "1", "v": "1", "X": "1", "q": "2", "f": "24", "o": "z", "m": "0"},
    ]


def test_shared_memory_holds_the_pixels_and_says_how_many(shm):
    pixels = RED * (40 * 30)
    text = frame_commands(7, True, 40, 30, [(0, 0, 40, 30, pixels)], shm)
    ((keys, payload),) = parsed(text)
    assert keys == {"a": "t", "i": "7", "s": "40", "v": "30", "q": "2", "f": "24", "t": "s", "S": str(len(pixels))}
    assert read_shm(payload, len(pixels)) == pixels


def test_every_command_gets_an_object_of_its_own(shm):
    "kitty reads an object when it gets to the command: one reused could show the next frame."
    text = frame_commands(7, False, 64, 32, [(0, 0, 1, 1, RED), (1, 0, 1, 1, RED)], shm)
    names = [base64.b64decode(payload) for _keys, payload in parsed(text)]
    assert len(set(names)) == 2
    assert all(len(name) <= SHM_NAME_MAX for name in names)


def test_close_unlinks_what_kitty_never_read():
    carrier = ShmCarrier()
    name = carrier.write(RED)
    carrier.close()
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name[1:], track=False)


def test_the_probe_is_a_query_for_one_pixel(shm):
    ((keys, payload),) = parsed(shm_probe(shm))
    assert keys == {"a": "q", "i": str(SHM_PROBE_ID), "s": "1", "v": "1", "f": "24", "t": "s", "S": "3"}
    assert read_shm(payload, 3) == bytes(3)
