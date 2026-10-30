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
from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.client.terminal import TerminalClient
from pymux.kitty_frames import (
    SHM_NAME_MAX,
    SHM_PROBE_ID,
    Base64Carrier,
    Pictures,
    ShmCarrier,
    frame_commands,
    rgba,
    shm_probe,
)
from pymux.window_frames import FRAME_PART_BYTES, frame_parts, video_parts

#: One graphics command: its keys and its payload.
COMMAND = re.compile(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\")

RED = bytes([0xFF, 0, 0])
#: `RED` as the terminal gets it.
OPAQUE_RED = bytes([0xFF, 0, 0, 0xFF])


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
    assert keys == {"a": "t", "i": "7", "s": "40", "v": "30", "q": "2", "f": "32", "o": "z", "m": "0"}
    assert zlib.decompress(base64.b64decode(payload)) == OPAQUE_RED * (40 * 30)


def test_rgb_becomes_opaque_rgba():
    assert rgba(bytes([1, 2, 3, 4, 5, 6])) == bytes([1, 2, 3, 255, 4, 5, 6, 255])
    assert rgba(b"") == b""


def test_a_large_frame_goes_in_chunks_kitty_takes():
    pixels = bytes(range(256)) * 3000
    text = frame_commands(7, True, 1000, 256, [(0, 0, 1000, 256, pixels)], Base64Carrier())
    chunks = COMMAND.findall(text)
    assert len(chunks) > 1
    assert all(len(payload) <= 4096 for _keys, payload in chunks)
    assert [keys.endswith("m=1") or keys == "m=1" for keys, _ in chunks[:-1]] == [True] * (len(chunks) - 1)
    assert chunks[-1][0].endswith("m=0")
    ((_keys, payload),) = parsed(text)
    assert zlib.decompress(base64.b64decode(payload)) == rgba(pixels)


def test_damage_goes_as_edits_of_the_root_frame():
    "The keys `pysmithay/src/kitty.rs` writes, so a terminal sees one protocol."
    rects = [(10, 5, 4, 4, RED * 16), (0, 0, 1, 1, RED)]
    text = frame_commands(7, False, 64, 32, rects, Base64Carrier())
    got = parsed(text)
    assert [keys for keys, _ in got] == [
        {
            "a": "f",
            "i": "7",
            "r": "1",
            "x": "10",
            "y": "5",
            "s": "4",
            "v": "4",
            "X": "1",
            "C": "1",
            "q": "2",
            "f": "32",
            "o": "z",
            "m": "0",
        },
        {
            "a": "f",
            "i": "7",
            "r": "1",
            "x": "0",
            "y": "0",
            "s": "1",
            "v": "1",
            "X": "1",
            "C": "1",
            "q": "2",
            "f": "32",
            "o": "z",
            "m": "0",
        },
    ]


BLUE = bytes([0, 0, 0xFF])


def test_a_frame_of_many_rectangles_becomes_their_bounding_box():
    "kitty uploads the whole image per edit. Lillecarl/pymux#644."
    pictures = Pictures()
    assert pictures.merged(7, True, 4, 3, [(0, 0, 4, 3, BLUE * 12)]) == [(0, 0, 4, 3, BLUE * 12)]
    merged = pictures.merged(7, False, 4, 3, [(0, 0, 1, 1, RED), (2, 1, 2, 1, RED * 2)])
    assert merged == [(0, 0, 4, 2, RED + BLUE * 3 + BLUE * 2 + RED * 2)]


def test_one_rectangle_goes_as_it_is_and_still_lands_in_the_copy():
    pictures = Pictures()
    pictures.merged(7, True, 2, 1, [(0, 0, 2, 1, BLUE * 2)])
    assert pictures.merged(7, False, 2, 1, [(1, 0, 1, 1, RED)]) == [(1, 0, 1, 1, RED)]
    assert pictures.merged(7, False, 2, 1, [(0, 0, 1, 1, RED), (0, 0, 1, 1, RED)]) == [(0, 0, 1, 1, RED)]
    assert pictures.merged(7, False, 2, 1, [(0, 0, 1, 1, BLUE), (1, 0, 1, 1, BLUE)]) == [(0, 0, 2, 1, BLUE * 2)]


def test_without_a_copy_the_rectangles_go_one_by_one():
    "No whole frame came yet, or the size changed since."
    pictures = Pictures()
    rects = [(0, 0, 1, 1, RED), (1, 0, 1, 1, RED)]
    assert pictures.merged(7, False, 2, 1, rects) == rects
    pictures.merged(7, True, 2, 1, [(0, 0, 2, 1, BLUE * 2)])
    assert pictures.merged(7, False, 3, 1, rects) == rects


def test_shared_memory_holds_the_pixels_and_says_how_many(shm):
    pixels = RED * (40 * 30)
    text = frame_commands(7, True, 40, 30, [(0, 0, 40, 30, pixels)], shm)
    ((keys, payload),) = parsed(text)
    assert keys == {"a": "t", "i": "7", "s": "40", "v": "30", "q": "2", "f": "32", "t": "s", "S": str(40 * 30 * 4)}
    assert read_shm(payload, 40 * 30 * 4) == OPAQUE_RED * (40 * 30)


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


def test_a_frame_goes_in_parts_of_whole_rows():
    "No part over `FRAME_PART_BYTES`, and the bands put back together are the rectangle."
    width, height = 300, 200
    pixels = bytes(range(256)) * (width * height * 3 // 256) + bytes(width * height * 3 % 256)
    parts = frame_parts(9, (4, True, width, height, [(0, 0, width, height, pixels)]), "PUT")
    assert len(parts) > 1
    for part in parts:
        assert sum(len(band[4]) for band in part[Field.RECTS]) <= FRAME_PART_BYTES
        assert part[Field.CMD] == Packet.WINDOW_FRAME and part[Field.IMAGE] == 9
    assert [bool(part.get(Field.LAST)) for part in parts] == [False] * (len(parts) - 1) + [True]
    assert parts[-1][Field.AFTER] == "PUT"
    bands = [band for part in parts for band in part[Field.RECTS]]
    assert b"".join(band[4] for band in bands) == pixels
    assert [band[1] for band in bands] == sorted(band[1] for band in bands)
    assert all(band[0] == 0 and band[2] == width and len(band[4]) == band[2] * band[3] * 3 for band in bands)


def test_an_encoded_frame_goes_in_parts_with_what_changed_at_the_end():
    "Lillecarl/pymux#639."
    data = bytes(range(256)) * (FRAME_PART_BYTES // 100)
    parts = video_parts(9, "vp9", (4, False, 64, 32, data, [(0, 0, 16, 16)]), "PUT")
    assert len(parts) > 1
    assert all(len(part[Field.VIDEO]) <= FRAME_PART_BYTES for part in parts)
    assert b"".join(part[Field.VIDEO] for part in parts) == data
    assert all(part[Field.CODEC] == "vp9" and not part[Field.WHOLE] for part in parts)
    assert [Field.RECTS in part for part in parts] == [False] * (len(parts) - 1) + [True]
    assert parts[-1][Field.RECTS] == [[0, 0, 16, 16]] and parts[-1][Field.AFTER] == "PUT"


class _Decoder:
    "What a decoder answers: the rectangles asked for, red, after `decoded` frames."

    def __init__(self) -> None:
        self.data: list[bytes] = []

    def decode(self, data, rects):
        self.data.append(data)
        return 64, 32, [(x, y, w, h, RED * (w * h)) for x, y, w, h in rects]


class _Client(TerminalClient):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    def _send_packet(self, data) -> None:
        self.sent.append(data)


@pytest.fixture
def terminal(capfd):
    "A client, and what it wrote to its terminal since the last `read()`."
    return _Client(), lambda: capfd.readouterr().out


def test_the_client_writes_a_frame_at_its_last_part(terminal):
    client, read = terminal
    pixels = RED * (20 * 10)
    parts = frame_parts(9, (1, True, 20, 10, [(0, 0, 20, 10, pixels)]), "\x1b_Ga=p,i=9\x1b\\")
    for part in parts[:-1]:
        client._process(part)
    assert read() == ""
    client._process(parts[-1])
    written = read()
    assert written.startswith("\x1b7") and written.endswith("\x1b_Ga=p,i=9\x1b\\\x1b8")
    ((keys, payload),) = parsed(written)
    assert (keys["a"], keys["i"], keys["s"], keys["v"]) == ("t", "9", "20", "10")
    assert zlib.decompress(base64.b64decode(payload)) == rgba(pixels)
    assert client.placed_images


def test_an_answered_frame_asks_the_terminal_on_its_last_command_only():
    "Lillecarl/pymux#643."
    rects = [(0, 0, 1, 1, RED), (1, 0, 1, 1, RED)]
    keys = [keys for keys, _ in parsed(frame_commands(7, False, 64, 32, rects, Base64Carrier(), answer=True))]
    assert [one.get("q") for one in keys] == ["2", None]
    whole = parsed(frame_commands(7, True, 1, 1, [(0, 0, 1, 1, RED)], Base64Carrier(), answer=True))
    assert "q" not in whole[0][0]


def test_a_frame_the_terminal_answers_is_not_answered_by_the_client(terminal):
    "Lillecarl/pymux#643."
    client, read = terminal
    part = frame_parts(9, (1, False, 20, 10, [(2, 3, 1, 1, RED)]), "")[0]
    client._process({**part, Field.ANSWER: True})
    ((keys, _payload),) = parsed(read())
    assert "q" not in keys
    assert client.sent == []


def test_a_frame_with_nothing_to_write_is_answered_by_the_client(terminal):
    client, read = terminal
    part = frame_parts(9, (1, False, 20, 10, []), "")[0]
    client._process({**part, Field.ANSWER: True})
    assert read() == ""
    assert client.sent == [{Field.CMD: Packet.FRAME_SHOWN}]


def test_the_client_sends_a_frame_of_two_edits_as_one(terminal):
    "Lillecarl/pymux#644."
    client, read = terminal
    client._process(frame_parts(9, (1, True, 20, 10, [(0, 0, 20, 10, RED * 200)]), "")[0])
    read()
    client._process(frame_parts(9, (2, False, 20, 10, [(1, 1, 1, 1, RED), (5, 6, 2, 2, RED * 4)]), "")[0])
    ((keys, _payload),) = parsed(read())
    assert (keys["x"], keys["y"], keys["s"], keys["v"]) == ("1", "1", "6", "7")


def test_the_client_answers_each_frame_at_its_last_part(terminal):
    "Lillecarl/pymux#641."
    client, read = terminal
    pixels = bytes(range(256)) * 3000
    parts = frame_parts(9, (1, True, 1000, 256, [(0, 0, 1000, 256, pixels[: 1000 * 256 * 3])]), "")
    assert len(parts) > 1
    for part in parts[:-1]:
        client._process(part)
    assert client.sent == []
    client._process(parts[-1])
    assert client.sent == [{Field.CMD: Packet.FRAME_SHOWN}]
    read()


def test_a_frame_that_will_not_decode_is_answered_all_the_same(terminal):
    client, _read = terminal

    class Broken:
        def decode(self, data, rects):
            raise ValueError("lost")

    client._decoder_kinds = {"vp9": Broken}
    part = video_parts(9, "vp9", (1, False, 64, 32, b"x", [(0, 0, 16, 16)]), "")[0]
    client._process({**part, Field.ANSWER: True})
    assert client.sent == [{Field.CMD: Packet.FRAME_SHOWN}]


def test_the_client_decodes_a_frame_and_writes_what_changed(terminal):
    "Lillecarl/pymux#639."
    client, read = terminal
    decoders: list[_Decoder] = []
    client._decoder_kinds = {"vp9": lambda: decoders.append(_Decoder()) or decoders[-1]}
    data = b"x" * (FRAME_PART_BYTES + 10)
    for part in video_parts(9, "vp9", (1, False, 64, 32, data, [(2, 3, 4, 5)]), ""):
        client._process(part)
    ((keys, payload),) = parsed(read())
    assert (keys["a"], keys["x"], keys["y"], keys["s"], keys["v"]) == ("f", "2", "3", "4", "5")
    assert zlib.decompress(base64.b64decode(payload)) == OPAQUE_RED * 20
    ((decoder,),) = [decoders]
    assert decoder.data == [data]


def test_a_keyframe_is_the_whole_image_again(terminal):
    client, read = terminal
    client._decoder_kinds = {"vp9": _Decoder}
    client._process(video_parts(9, "vp9", (1, True, 64, 32, b"k", [(0, 0, 64, 32)]), "\x1b_Ga=p,i=9\x1b\\")[0])
    ((keys, _payload),) = parsed(read())
    assert (keys["a"], keys["s"], keys["v"]) == ("t", "64", "32")


def test_a_frame_that_will_not_decode_writes_nothing(terminal):
    client, read = terminal

    class Broken:
        def decode(self, data, rects):
            raise ValueError("lost")

    client._decoder_kinds = {"vp9": Broken}
    client._process(video_parts(9, "vp9", (1, False, 64, 32, b"x", [(0, 0, 16, 16)]), "")[0])
    assert read() == ""
    assert not client._decoders


def test_the_client_takes_shared_memory_once_the_terminal_read_it(terminal):
    client, read = terminal
    client._shm_probe()
    client._process({Field.CMD: Packet.SHM})
    client._process(frame_parts(9, (1, False, 20, 10, [(2, 3, 1, 1, RED)]), "")[0])
    ((keys, payload),) = parsed(read())
    assert keys["t"] == "s"
    assert read_shm(payload, 4) == OPAQUE_RED
    client._shm.close()


async def test_the_server_says_when_the_terminal_read_the_probe():
    got: list[dict] = []
    async with over_connection(read_packet=got.append) as session:
        state, _size = await session.attach("one", Size(rows=24, columns=80), frames=True)
        state.connection.graphics.handle_reply("\x1b_Gi=%d;OK\x1b\\" % SHM_PROBE_ID)
        await once(lambda: any(packet.get("cmd") == "shm" for packet in got), 5.0, "no shm packet")
        # An error is not an answer that it can.
        state.connection.graphics.handle_reply("\x1b_Gi=%d;EBADF:no\x1b\\" % SHM_PROBE_ID)
        assert not state.connection.graphics.kitty_supported


def test_the_probe_is_a_query_for_one_pixel(shm):
    ((keys, payload),) = parsed(shm_probe(shm))
    assert keys == {"a": "q", "i": str(SHM_PROBE_ID), "s": "1", "v": "1", "f": "32", "t": "s", "S": "4"}
    assert read_shm(payload, 4) == bytes(4)
