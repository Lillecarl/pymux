"""
A clipboard read across a peer link: the owner of a pane asks the peer
whose person used it last for that person's clipboard, when a program
here pastes through the compositor (Chromium's Ctrl+V, wl-paste). The
person is no client here, so without this the read never reaches them.
Lillecarl/pyterm#992.

Each side gates it with its own `set-clipboard`: the owner asks only
under "on", and the peer answers only under "on".
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from pyte.keep import Keep

from ..enums import Clipboard

if TYPE_CHECKING:
    from ..core.main import Pymux

__all__ = ["ClipboardAsks", "clipboard_answer"]


class ClipboardAsks:
    "The `clipboard-ask` requests the owner's end of a link sent and waits on, by id."

    KEEP: ClassVar[dict[str, Keep]] = {
        # A paste that waits hears that the link went.
        "_send": Keep.REBUILT,
        "_waiting": Keep.DROPPED,
    }

    def __init__(self, send: Callable[[Message], None]) -> None:
        self._send = send
        self._waiting: dict[str, tuple[anyio.Event, list[str | None]]] = {}

    async def ask(self, selection: str, timeout: float) -> str | None:
        "The text of the peer person's selection, OSC 52's letter `selection`, or None for no answer."
        request = secrets.token_hex(8)
        done = anyio.Event()
        answer: list[str | None] = []
        self._waiting[request] = (done, answer)
        try:
            self._send({Field.CMD: Packet.CLIPBOARD_ASK, Field.REQUEST: request, Field.DATA: selection})
            with anyio.move_on_after(timeout):
                await done.wait()
        finally:
            self._waiting.pop(request, None)
        return answer[0] if answer else None

    def answered(self, packet: Message) -> None:
        waiting = self._waiting.get(str(packet.get(Field.REQUEST)))
        if waiting is not None:
            done, answer = waiting
            text = packet.get(Field.DATA)
            answer.append(text if isinstance(text, str) else None)
            done.set()

    def gone(self) -> None:
        "The link went: nothing waits for it any more."
        for done, answer in self._waiting.values():
            answer.append(None)
            done.set()


async def clipboard_answer(pymux: Pymux, packet: Message) -> Message:
    "The `clipboard-answer` to an owner's `clipboard-ask`: the text of the selection, or none."
    reply: Message = {Field.CMD: Packet.CLIPBOARD_ANSWER, Field.REQUEST: packet.get(Field.REQUEST)}
    client = pymux.latest_client()
    if pymux.options.set_clipboard is not Clipboard.ON or client is None:
        return reply
    text = await client.client.clipboard.read(str(packet.get(Field.DATA) or "c")[:1])
    if text is not None:
        reply[Field.DATA] = text
    return reply
