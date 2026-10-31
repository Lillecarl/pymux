"""
Files a pane asks for on this machine: `pymux receive`. Lillecarl/pymux#743.

The client looks at the paths and its own settings off the loop. It
sends what a grant or `upload-paths` lets pass, and asks the person
about the rest, on the last row of the terminal.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from libpymux.protocol import Field, Message, Packet
from pyte import escape
from pyte.parameters import EraseExtent
from pyte.sequences.csi import csi
from pyte.sequences.rendition import Sgr, sgr

from pymux.config import client_options_in, find_config
from pymux.graphics.graphics import RESTORE_CURSOR, SAVE_CURSOR

from .uploads import (
    ESCAPE,
    Answer,
    UploadSettings,
    Wanted,
    allowed,
    answer_of,
    done_packet,
    inspect,
    listed_packet,
    listing,
    outcome_packets,
    question,
    upload_settings,
)

if TYPE_CHECKING:
    from .terminal import TerminalClient

__all__ = ["BUSY", "SAID_NO", "Asking", "SendingFiles"]

REVERSE = sgr(Sgr.REVERSE)
NO_ATTRIBUTES = sgr()


def _row(number: int) -> str:
    "Go to the start of a row, and erase it."
    return csi(escape.CUP, number, 1) + csi(escape.EL, EraseExtent.ALL)


#: Why a request is refused without a question.
BUSY = "the client is asking the person about another request"
SAID_NO = "the person said no"


@dataclass(slots=True)
class Asking:
    """
    A request the person has not answered yet. `flags_after` holds the
    keyboard flags the server asked for meanwhile.
    """

    request: str
    wanted: list[Wanted]
    text: str
    grant_seconds: float
    flags_before: int | None
    flags_after: int | None = None


class SendingFiles:
    "The files a pane asks this client for, and the question about them."

    def __init__(self, client: TerminalClient) -> None:
        self.client = client
        #: The request the person is being asked about, and until when
        #: "a" lets the server read without asking. A client serves one
        #: server, so the grant is that server's.
        self.asking: Asking | None = None
        self.grant_until: float | None = None
        #: Requests whose paths are being looked at, off the loop. One the
        #: server withdraws meanwhile leaves, and is never asked about.
        self.looking: set[str] = set()
        #: The clock a grant runs on.
        self.clock: Callable[[], float] = time.monotonic

    def connection_started(self) -> None:
        "A question about a request of the last connection has nobody to answer to."
        self.looking.clear()
        if self.asking is not None:
            self.stop_asking()

    def hold_flags(self, flags: int) -> bool:
        "A question takes plain keys: keep the keyboard flags for after it. False when none shows."
        if self.asking is None:
            return False
        self.asking.flags_after = flags
        return True

    def off_the_loop[T](self, work: Callable[[], T], then: Callable[[T], None]) -> None:
        """
        Run `work`, which blocks, away from the loop that draws, and
        `then` with its result. A client on a loop runs `then` on it.
        The socket client has no loop, so `then` runs on the thread.
        """
        tasks = self.client.tasks
        if tasks is not None:

            async def on_the_loop() -> None:
                result = await anyio.to_thread.run_sync(work)
                with contextlib.suppress(BrokenPipeError):
                    then(result)

            tasks.start_soon(on_the_loop)
            return

        def on_a_thread() -> None:
            then(work())

        threading.Thread(target=on_a_thread, name="pymux-upload", daemon=True).start()

    def settings(self) -> UploadSettings:
        "What this client's own configuration file says. **It blocks.**"
        return upload_settings(client_options_in(self.client.config_file or find_config()), Path.home())

    def granted(self) -> bool:
        return self.grant_until is not None and self.clock() < self.grant_until

    def asked(self, packet: Message) -> None:
        """
        A pane asks for files on this machine. Look at the paths and
        the settings off the loop, then send or ask.
        """
        request = str(packet.get(Field.REQUEST) or "")
        paths = [str(path) for path in packet.get(Field.PATHS) or []]
        pane = str(packet.get(Field.PANE) or "?")
        if self.asking is not None:
            self.client.send_packet(done_packet(request, BUSY))
            return

        def look() -> tuple[UploadSettings, list[Wanted]]:
            return self.settings(), inspect(paths, Path.home())

        def then(found: tuple[UploadSettings, list[Wanted]]) -> None:
            if request in self.looking:
                self.looking.discard(request)
                self.decide(request, pane, *found)

        self.looking.add(request)
        self.off_the_loop(look, then)

    def listing_asked(self, packet: Message) -> None:
        "A completion of `pymux receive` asks what a directory here holds. Nobody is asked."
        request = str(packet.get(Field.REQUEST) or "")
        asked = str(packet.get(Field.PATH) or "")
        granted = self.granted()

        def look() -> list[str]:
            return listing(asked, Path.home(), self.settings().roots, granted)

        self.off_the_loop(look, lambda names: self.client.send_packet(listed_packet(request, names)))

    def withdrawn(self, request: str) -> None:
        "The `pymux receive` that asked stopped waiting: ask nothing more about it."
        self.looking.discard(request)
        if self.asking is not None and self.asking.request == request:
            self.stop_asking()

    def decide(self, request: str, pane: str, settings: UploadSettings, wanted: list[Wanted]) -> None:
        "Send what a grant or `upload-paths` lets pass, and ask the person about the rest."
        client = self.client
        if self.granted() or allowed(wanted, settings.roots):
            self.send(request, wanted)
            return
        if self.asking is not None:
            client.send_packet(done_packet(request, BUSY))
            return
        _rows, columns = client.size()
        text = question(pane, client.server_host(), wanted, settings.grant_seconds, Path.home(), columns)
        self.asking = Asking(request, wanted, text, settings.grant_seconds, client.kitty_flags)
        # The answer has to arrive as a plain key.
        if client.kitty_flags:
            client.set_kitty_flags(0)
        self.draw_question()

    def draw_question(self) -> None:
        "Draw the question on the last row, and put the cursor back."
        asking = self.asking
        if asking is None:
            return
        rows, _columns = self.client.size()
        drawn = SAVE_CURSOR + _row(rows) + REVERSE + asking.text + NO_ATTRIBUTES + RESTORE_CURSOR
        self.client.write_terminal(drawn.encode("utf-8"))

    def stop_asking(self) -> Asking | None:
        "Take the question off the terminal, and ask the server to draw over where it was."
        asking, self.asking = self.asking, None
        if asking is None:
            return None
        client = self.client
        rows, _columns = client.size()
        client.write_terminal((SAVE_CURSOR + _row(rows) + RESTORE_CURSOR).encode("ascii"))
        flags = asking.flags_after if asking.flags_after is not None else asking.flags_before
        if flags:
            client.set_kitty_flags(flags)
        with contextlib.suppress(BrokenPipeError):
            client.send_packet({Field.CMD: Packet.REDRAW})
        return asking

    def answer(self, keys: str) -> bool:
        """
        Take one read of the keyboard as the answer to the question.
        False when it is for the server instead.

        **A sequence goes on.** The server waits for the answers of the
        terminal, a status report among them, and they arrive here as
        input. A key that the question takes never starts with an
        escape: the keyboard flags are off while it shows.
        """
        if keys.startswith(ESCAPE) and keys != ESCAPE:
            return False
        answer = answer_of(keys)
        if answer is None:
            return True
        asking = self.stop_asking()
        if asking is None:
            return True
        if answer is Answer.NO:
            self.client.send_packet(done_packet(asking.request, SAID_NO))
            return True
        if answer is Answer.GRANT:
            self.grant_until = self.clock() + asking.grant_seconds
        self.send(asking.request, asking.wanted)
        return True

    def send(self, request: str, wanted: list[Wanted]) -> None:
        "Read each file off the loop, one at a time, and send it."
        tasks = self.client.tasks
        if tasks is not None:
            tasks.start_soon(self._send_from_the_loop, request, wanted)
            return
        threading.Thread(target=self._send_here, args=(request, wanted), name="pymux-upload", daemon=True).start()

    async def _send_from_the_loop(self, request: str, wanted: list[Wanted]) -> None:
        with contextlib.suppress(BrokenPipeError):
            for one in wanted:
                packets = await anyio.to_thread.run_sync(lambda one=one: list(outcome_packets(request, one)))
                for packet in packets:
                    self.client.send_packet(packet)
            self.client.send_packet(done_packet(request))

    def _send_here(self, request: str, wanted: list[Wanted]) -> None:
        with contextlib.suppress(BrokenPipeError):
            for one in wanted:
                for packet in outcome_packets(request, one):
                    self.client.send_packet(packet)
            self.client.send_packet(done_packet(request))
