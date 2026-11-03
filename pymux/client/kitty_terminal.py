"""
The pymux client as the terminal of kitty's file transfer, for a
terminal that is not kitty. Lillecarl/pymux#912.

`Transfers` does the work and blocks on the file system, so it runs off
the loop that draws, one step at a time and in the order the commands
came. While a program reads a file and no command waits, the next step
is the next part of that file, once the server says the pane's program
took the last one (`Packet.KITTY_TRANSFER_TAKEN`). The person answers through
`SendingFiles`, the question `pymux receive` asks.
"""

from __future__ import annotations

import contextlib
import math
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from libpymux.protocol import Field, Message, Packet
from pyte.sequences.file_transfer import TransferCommand

from pymux.config import client_options_in, find_config

from .kitty_transfer import Effect, Question, Transfers, Unasked, absolute
from .uploads import Answer, UploadSettings, all_under, fitted, grant_answers, home_as_tilde, upload_settings

if TYPE_CHECKING:
    from .terminal import TerminalClient

__all__ = ["BUSY", "KittyTerminal"]

type _Step = Callable[[Transfers], list[Effect]]

#: Before a session's id in a question's request, so it never meets the
#: id of a `pymux receive`.
_REQUEST: Final = "kitty:"

#: Why a session is refused without a question.
BUSY: Final = "pymux is asking the person about something else"


class KittyTerminal:
    "The sessions of one client, and the worker that serves them."

    def __init__(self, client: TerminalClient) -> None:
        self.client = client
        #: The steps to the worker: a stream to a task on the client's
        #: loop, or a queue to a thread for a client with none.
        self._stream: MemoryObjectSendStream[_Step] | None = None
        self._stream_of: anyio.abc.TaskGroup | None = None
        self._queue: queue.SimpleQueue[_Step | None] | None = None
        #: How long "a" grants, as the configuration said when last read.
        self._grant_seconds = 0.0
        #: The session whose last part the server has not said taken.
        self._waiting: str | None = None

    def said(self, packet: Message) -> None:
        "A command of a pane's program."
        param = str(packet.get(Field.DATA) or "")
        pane = str(packet.get(Field.PANE) or "?")
        self._run(lambda transfers: transfers.take(param, pane))

    def taken(self, packet: Message) -> None:
        "The server says a pane's program took the last part: the next may go."
        session_id = str(packet.get(Field.DATA) or "")

        def step(transfers: Transfers) -> list[Effect]:
            if self._waiting == session_id:
                self._waiting = None
            return []

        self._run(step)

    def connection_started(self) -> None:
        "The sessions of the last connection have nobody to answer: their files close."
        self._waiting = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._queue is not None:
            self._queue.put(None)
            self._queue = None

    def _run(self, step: _Step) -> None:
        tasks = self.client.tasks
        if tasks is not None:
            if self._stream is None or self._stream_of is not tasks:
                self._stream, receive = anyio.create_memory_object_stream[_Step](math.inf)
                self._stream_of = tasks
                tasks.start_soon(self._serve_on_the_loop, receive)
            with contextlib.suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
                self._stream.send_nowait(step)
            return
        if self._queue is None:
            self._queue = queue.SimpleQueue()
            threading.Thread(
                target=self._serve_on_a_thread, args=(self._queue,), name="pymux-kitty-transfer", daemon=True
            ).start()
        self._queue.put(step)

    def _transfers(self) -> Transfers:
        return Transfers(Path.home(), self._may_read, bypass=self._bypass)

    def _bypass(self) -> str:
        "`file-transfer-bypass` from this client's own file. **It blocks.**"
        return self._settings().bypass

    async def _serve_on_the_loop(self, steps: MemoryObjectReceiveStream[_Step]) -> None:
        transfers = self._transfers()
        try:
            with steps:
                while True:
                    try:
                        step = steps.receive_nowait()
                    except anyio.WouldBlock:
                        if self._pumps(transfers):
                            step = Transfers.pump
                        else:
                            try:
                                step = await steps.receive()
                            except anyio.EndOfStream:
                                return
                    except anyio.EndOfStream:
                        return
                    effects = await anyio.to_thread.run_sync(step, transfers)
                    with contextlib.suppress(BrokenPipeError):
                        self._apply(effects, pumped=step is Transfers.pump)
        finally:
            transfers.close()

    def _serve_on_a_thread(self, steps: queue.SimpleQueue[_Step | None]) -> None:
        transfers = self._transfers()
        try:
            while True:
                try:
                    step = steps.get_nowait()
                except queue.Empty:
                    step = Transfers.pump if self._pumps(transfers) else steps.get()
                if step is None:
                    return
                with contextlib.suppress(BrokenPipeError):
                    self._apply(step(transfers), pumped=step is Transfers.pump)
        finally:
            transfers.close()

    def _may_read(self, paths: list[str]) -> bool:
        "Whether a grant or `upload-paths` lets these pass. **It blocks.**"
        settings = self._settings()
        self._grant_seconds = settings.grant_seconds
        return self.client.sending_files.granted() or all_under(paths, settings.roots)

    def _pumps(self, transfers: Transfers) -> bool:
        return transfers.has_data and self._waiting is None

    def _settings(self) -> UploadSettings:
        return upload_settings(client_options_in(self.client.config_file or find_config()), Path.home())

    def _apply(self, effects: list[Effect], pumped: bool = False) -> None:
        for effect in effects:
            match effect:
                case TransferCommand():
                    self.client.send_packet({Field.CMD: Packet.KITTY_TRANSFER, Field.DATA: effect.param()})
                case Question():
                    self._ask(effect)
                case Unasked():
                    self.client.sending_files.withdrawn(_REQUEST + effect.session)
        part_of = next((effect.id for effect in effects if isinstance(effect, TransferCommand)), None)
        if pumped and part_of is not None:
            self._waiting = part_of
            self.client.send_packet({Field.CMD: Packet.KITTY_TRANSFER_TAKEN, Field.DATA: part_of})

    def _ask(self, question: Question) -> None:
        client = self.client
        files = client.sending_files
        _rows, columns = client.size()
        host = client.server_host()
        if question.reads:
            home = Path.home()
            listed = ", ".join(home_as_tilde(Path(absolute(path, home)), home) for path in question.paths)
            text = fitted(
                "pane %s on %s asks for " % (question.pane, host), listed, grant_answers(self._grant_seconds), columns
            )
            answers = frozenset(Answer)
        else:
            text = fitted("pane %s on %s wants to write files here" % (question.pane, host), "", ": y / n", columns)
            answers = frozenset({Answer.ONCE})
        grant_seconds = self._grant_seconds

        def decided(answer: Answer) -> None:
            if answer is Answer.GRANT:
                files.grant(grant_seconds)
            self._run(lambda transfers: transfers.answered(question.session, answer is not Answer.NO))

        if not files.ask(_REQUEST + question.session, text, answers, decided):
            self._run(lambda transfers: transfers.answered(question.session, False, BUSY))
