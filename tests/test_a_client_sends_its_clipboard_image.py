"""
A client sends the image on its machine's clipboard ahead of a Ctrl-V,
and nothing typed after the key overtakes it. Lillecarl/pymux#956.
"""

from __future__ import annotations

import base64

import anyio
import pytest
from libpymux.protocol import Field, Packet
from test_a_pane_asks_for_a_file import AskedClient

from pymux.client import clipboard_image, terminal

IMAGE = b"\x89PNG not really"


@pytest.fixture
def client(tmp_path):
    config = tmp_path / "client.conf"
    config.write_text("")
    return AskedClient(config)


def kinds(client) -> list:
    return [(packet[Field.CMD], packet.get(Field.DATA)) for packet in client.sent]


@pytest.mark.parametrize("ctrl_v", ["\x16", "\x1b[118;5u"])
async def test_the_image_goes_ahead_of_the_key_and_what_follows_it(client, monkeypatch, ctrl_v):
    reading = anyio.Event()
    read = anyio.Event()

    async def slow_image():
        reading.set()
        await read.wait()
        return IMAGE

    monkeypatch.setattr(terminal, "clipboard_image", slow_image)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            client.tasks = tasks
            client._take_input("a")
            client._take_input(ctrl_v)
            await reading.wait()
            # Typed while the clipboard is read: it waits behind the key.
            client._take_input("b")
            assert kinds(client) == [(Packet.IN, "a")]
            read.set()

    assert kinds(client) == [
        (Packet.IN, "a"),
        (Packet.CLIPBOARD_IMAGE, base64.b64encode(IMAGE).decode("ascii")),
        (Packet.IN, ctrl_v),
        (Packet.IN, "b"),
    ]


async def test_no_image_says_so_and_withdraws_the_last_one(client, monkeypatch):
    async def none():
        return None

    monkeypatch.setattr(terminal, "clipboard_image", none)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            client.tasks = tasks
            client._take_input("\x16")
    assert kinds(client) == [(Packet.CLIPBOARD_IMAGE, ""), (Packet.IN, "\x16")]


def test_other_keys_send_no_image(client):
    client._take_input("v\x1b[A")
    assert kinds(client) == [(Packet.IN, "v\x1b[A")]


def test_osascripts_png_answer_is_read():
    said = "«data PNGf%s»\n" % IMAGE.hex().upper()
    found = clipboard_image._PNG_DATA.search(said)
    assert found is not None and bytes.fromhex(found.group(1)) == IMAGE
    assert clipboard_image._PNG_DATA.search("«class utf8»") is None
