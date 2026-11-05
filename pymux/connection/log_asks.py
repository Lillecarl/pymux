"""
`show-log` on another machine: the request one end sends over a
client's connection or a peer link, and the answer the other end
reads off its own disk. Lillecarl/pymux#934.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from pyte.keep import Keep

from .. import log
from ..pipes.base import BrokenPipeError

__all__ = ["LOG_SECONDS", "LogAsks", "log_answer"]

#: How long `show-log` waits for the far end. An end too old to know
#: `report-log` drops it unread, so this is also how long that takes.
LOG_SECONDS = 5.0

#: What `show-log` says when the far end let `LOG_SECONDS` pass.
NO_ANSWER = "no answer in %g seconds: it went, or its pymux is too old to send a log" % (LOG_SECONDS,)


class LogAsks:
    "The `report-log` requests one end sent and waits on, by id."

    KEEP: ClassVar[dict[str, Keep]] = {
        # A `show-log` that waits ends with the connection.
        "_send": Keep.REBUILT,
        "_waiting": Keep.DROPPED,
    }

    def __init__(self, send: Callable[[Message], None]) -> None:
        self._send = send
        self._waiting: dict[str, tuple[anyio.Event, list[Message]]] = {}

    async def ask(self, lines: int) -> str:
        "The far end's last `lines` lines of log. Raises `LookupError` saying why there are none."
        request = secrets.token_hex(8)
        done = anyio.Event()
        answer: list[Message] = []
        self._waiting[request] = (done, answer)
        try:
            try:
                self._send({Field.CMD: Packet.REPORT_LOG, Field.REQUEST: request, Field.LINES: lines})
            except BrokenPipeError:
                raise LookupError("it went before it was asked") from None
            with anyio.move_on_after(LOG_SECONDS):
                await done.wait()
        finally:
            self._waiting.pop(request, None)
        if not answer:
            raise LookupError(NO_ANSWER)
        if answer[0].get(Field.ERROR):
            raise LookupError(str(answer[0][Field.ERROR]))
        return str(answer[0].get(Field.DATA) or "")

    def answered(self, packet: Message) -> None:
        waiting = self._waiting.get(str(packet.get(Field.REQUEST)))
        if waiting is not None:
            done, answer = waiting
            answer.append(packet)
            done.set()

    def gone(self) -> None:
        "The far end went: nothing waits for it any more."
        for done, answer in self._waiting.values():
            answer.append({Field.ERROR: "it went before it answered"})
            done.set()


def log_answer(packet: Message) -> Message:
    "The `log` packet that answers a `report-log`. **It blocks**, reading the log."
    answer: Message = {Field.CMD: Packet.LOG, Field.REQUEST: packet.get(Field.REQUEST)}
    try:
        answer[Field.DATA] = log.tail(int(packet.get(Field.LINES) or 0))
    except (OSError, ValueError) as error:
        answer[Field.ERROR] = "cannot read the log: %s" % (error,)
    return answer
