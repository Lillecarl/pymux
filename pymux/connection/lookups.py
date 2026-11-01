"""
The suggestion and the completion a key starts on a line a person types
into, and the hold they put on the client's frames until they end.

prompt_toolkit clears both on every insert and looks them up again in
tasks, and the frame the key asked for is queued before those tasks
run. Without a hold that frame shows the line with the grey suggestion
and the completion menu gone, and the next frame brings them back.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterable
from typing import TYPE_CHECKING, override

from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import CompleteEvent, Completer, Completion

if TYPE_CHECKING:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document

    from .frame_holds import FrameHolds

__all__ = ["LOOKUP_HOLD", "Lookups"]

#: How long a frame waits for a lookup a key started. Both lookups here
#: read memory, so one that takes longer is not coming soon, and the
#: frame shows what there is.
LOOKUP_HOLD = 0.05

_Ended = Callable[[str, "Document"], None]


class Lookups:
    "The lookups one buffer starts on each insert, and the frame hold they keep while they run."

    def __init__(self, buffer: Buffer, holds: FrameHolds) -> None:
        self.buffer = buffer
        self.holds = holds
        self._key = ("lookup", buffer.name)
        self._running: set[str] = set()
        self._started_for: Document | None = None
        if buffer.auto_suggest is not None:
            buffer.auto_suggest = _EndingSuggest(buffer.auto_suggest, self._ended)
        if buffer.completer is not None:
            buffer.completer = _EndingCompleter(buffer.completer, self._ended)
        buffer.on_text_insert += self._inserted

    def _inserted(self, buffer: Buffer) -> None:
        "What `Buffer.insert_text` starts after this event, and only then."
        running = set()
        if buffer.auto_suggest is not None:
            running.add("suggestion")
        if buffer.completer is not None and buffer.complete_while_typing():
            running.add("completion")
        if not running:
            return
        self._running = running
        self._started_for = buffer.document
        self.holds.hold(self._key, lambda: not self._running, LOOKUP_HOLD)

    def _ended(self, kind: str, document: Document) -> None:
        # A lookup for older text ended; prompt_toolkit looks again for the new.
        if document != self._started_for:
            return
        self._running.discard(kind)
        if not self._running:
            self.holds.release(self._key)


class _EndingSuggest(AutoSuggest):
    "A suggester that says when a lookup ended."

    def __init__(self, inner: AutoSuggest, ended: _Ended) -> None:
        self.inner = inner
        self.ended = ended

    @override
    def get_suggestion(self, buffer: Buffer, document: Document) -> Suggestion | None:
        return self.inner.get_suggestion(buffer, document)

    @override
    async def get_suggestion_async(self, buff: Buffer, document: Document) -> Suggestion | None:
        try:
            return await self.inner.get_suggestion_async(buff, document)
        finally:
            self.ended("suggestion", document)


class _EndingCompleter(Completer):
    "A completer that says when a lookup ended."

    def __init__(self, inner: Completer, ended: _Ended) -> None:
        self.inner = inner
        self.ended = ended

    @override
    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        return self.inner.get_completions(document, complete_event)

    @override
    async def get_completions_async(
        self, document: Document, complete_event: CompleteEvent
    ) -> AsyncGenerator[Completion]:
        try:
            async for completion in self.inner.get_completions_async(document, complete_event):
                yield completion
        finally:
            self.ended("completion", document)
