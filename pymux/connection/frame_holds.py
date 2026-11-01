"""
What a client's next frame waits for.

**A frame that drops the old result before the new one is in
flickers.** The old result goes, one frame shows neither, and the next
frame shows the new one. A key clears the grey suggestion of the
command line and a lookup finds the next; a window a person switches to
shows a mirror's old page until the owner's catch-up frame comes. Each
of them holds the client's frames here until the new result is in, or
until a deadline passes, so a result that never comes costs one wait
and no more. Lillecarl/pymux#759.

The barrier is per client: a person on another window has nothing to
wait for.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING, NamedTuple

import anyio

if TYPE_CHECKING:
    import anyio.abc

__all__ = ["FrameHolds"]


class _Hold(NamedTuple):
    done: Callable[[], bool]
    deadline: float


class FrameHolds:
    "The results one client's next frame waits for, each until it is in or its deadline passed."

    def __init__(self, wake: Callable[[], None], tasks: Callable[[], anyio.abc.TaskGroup | None]) -> None:
        #: Asks the client for a frame: at a deadline, and when a result
        #: is in that nothing else redraws for.
        self._wake = wake
        self._tasks = tasks
        self._holds: dict[Hashable, _Hold] = {}

    def hold(self, key: Hashable, done: Callable[[], bool], within: float) -> None:
        "Hold the frames until `done()` says the result is in, for `within` seconds at most. A key held again starts again."
        self._holds[key] = _Hold(done, time.monotonic() + within)
        tasks = self._tasks()
        if tasks is not None:
            tasks.start_soon(self._wake_after, within)

    def waiting(self) -> bool:
        "Whether a result is still on its way. Each one that is in, or whose deadline passed, goes."
        if not self._holds:
            return False
        now = time.monotonic()
        for key, hold in list(self._holds.items()):
            if now >= hold.deadline or hold.done():
                del self._holds[key]
        return bool(self._holds)

    def release(self, key: Hashable) -> None:
        "The result of `key` is in: draw, unless another holds the frame still."
        if self._holds.pop(key, None) is not None:
            self._wake()

    async def _wake_after(self, seconds: float) -> None:
        await anyio.sleep(seconds)
        self._wake()
