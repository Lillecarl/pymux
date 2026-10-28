"""
Files a pane asks for from the person's machine: `pymux receive`.
Lillecarl/pymux#743, phase 2.

**The pymux client decides, not the server.** The server runs on the far
machine with whatever runs in its panes, so anything it drew or held
could be faked by a program there. The client asks the person on its own
terminal, holds any grant, and reads its allow-list from its own
configuration file: `pymux.client.uploads`. The server only asks, joins
the parts the client sends, and writes each file into the directory the
pane named, never over a file that is there.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import anyio
from libpymux.protocol import Field, Message, Packet

from .files import ArrivingFiles, Whole, save

__all__ = [
    "LIST_SECONDS",
    "Listing",
    "Upload",
    "receive_into",
]

#: How long a completion waits for the client's listing. A shell holds
#: the Tab until it ends.
LIST_SECONDS = 2.0

#: What `pymux receive` says for a path the client never answered.
NO_ANSWER = "the client sent nothing for it"


@dataclass(frozen=True, slots=True)
class _Arrived:
    asked: str
    whole: Whole


@dataclass(frozen=True, slots=True)
class _NotSent:
    asked: str
    why: str


@dataclass(frozen=True, slots=True)
class _Ended:
    why: str | None


type _Outcome = _Arrived | _NotSent | _Ended


class Upload:
    """
    One request, from `send-files` to `upload-done`.

    The client's packets arrive on its own connection, and the command
    that waits is another one, so the two meet in the stream here. It
    has no bound: a file is held whole until the command has written it.
    """

    def __init__(self, paths: list[str]) -> None:
        self.request = secrets.token_hex(8)
        self.paths = paths
        self._parts = ArrivingFiles()
        self._send, self.outcomes = anyio.create_memory_object_stream[_Outcome](math.inf)
        #: The client said it sent all it will, or it went.
        self.ended = False

    def packet(self, pane: str) -> Message:
        "The `send-files` packet that asks the client."
        return {Field.CMD: Packet.SEND_FILES, Field.REQUEST: self.request, Field.PATHS: self.paths, Field.PANE: pane}

    def take(self, packet: Mapping[str, object]) -> None:
        "One `upload` or `upload-done` packet of this request."
        error = packet.get(Field.ERROR)
        if packet.get(Field.CMD) == Packet.UPLOAD_DONE:
            self.end(str(error) if error else None)
            return
        asked = str(packet.get(Field.PATH) or "")
        if error:
            self._put(_NotSent(asked, str(error)))
            return
        arrived = self._parts.receive(packet)
        if isinstance(arrived, Whole):
            self._put(_Arrived(asked, arrived))
        elif arrived is not None:
            self._put(_NotSent(asked, arrived))

    def end(self, why: str | None) -> None:
        "Nothing more arrives: the client said so, or it went."
        self.ended = True
        self._put(_Ended(why))
        self._send.close()

    def _put(self, outcome: _Outcome) -> None:
        # A part after the end, or after the command stopped waiting.
        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            self._send.send_nowait(outcome)


class Listing:
    "One `list-files` question, until the client answers or goes."

    def __init__(self, path: str) -> None:
        self.request = secrets.token_hex(8)
        self.path = path
        self.names: list[str] = []
        self.answered = anyio.Event()

    def packet(self) -> Message:
        return {Field.CMD: Packet.LIST_FILES, Field.REQUEST: self.request, Field.PATH: self.path}

    def take(self, packet: Mapping[str, object]) -> None:
        names = packet.get(Field.PATHS)
        if isinstance(names, list):
            self.names = [str(name) for name in names]
        self.answered.set()


async def receive_into(upload: Upload, directory: Path, say: Callable[[str], None]) -> list[str]:
    """
    Write each file into `directory` as it arrives, and say where it
    went. Returns why each path that did not arrive did not.

    A path is answered once. A second answer for it, or one for a path
    nobody asked for, is dropped.
    """
    waiting: list[str] = list(upload.paths)
    failed: list[str] = []
    with upload.outcomes:
        async for outcome in upload.outcomes:
            if isinstance(outcome, _Ended):
                if outcome.why is not None:
                    failed.extend("%s: %s" % (asked, outcome.why) for asked in waiting)
                    waiting = []
                break
            if outcome.asked not in waiting:
                continue
            waiting.remove(outcome.asked)
            if isinstance(outcome, _NotSent):
                failed.append("%s: %s" % (outcome.asked, outcome.why))
                continue
            try:
                path = await anyio.to_thread.run_sync(save, directory, outcome.whole.name, outcome.whole.data)
            except OSError as error:
                failed.append("%s: %s" % (outcome.asked, error))
                continue
            say("Saved %s." % (path,))
    failed.extend("%s: %s" % (asked, NO_ANSWER) for asked in waiting)
    return failed
