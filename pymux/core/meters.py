"""
The meters a status line shows: rates read off counts that only go up.
Lillecarl/pyterm#996.

**Sampled on a clock of their own, once a second.** A meter sampled
when the status line read it moved only when something drew, so a
reading lagged by up to `status-interval` plus the window, and an idle
client kept the last reading it drew.

**A client redraws when a meter it shows would read differently**, and
only then. Each frame notes the readings it drew (`ClientApplication.
meters_shown`); after a sample, a client whose readings moved asks for a
frame that only time asked for, which no meter counts. So an idle client
reads zero once and then sends nothing.
"""

from __future__ import annotations

import time
import weakref
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

import anyio
from pyte.keep import Keep

from ..log import logger

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["RATE_WINDOW", "SAMPLE_SECONDS", "Meter", "Meters"]

#: How often every meter is sampled, in seconds.
SAMPLE_SECONDS = 1.0

#: How far back a rate looks, in seconds: one burst reads as a rate for
#: this long, and a client that went quiet reads zero this long after.
RATE_WINDOW = 3.0


class Meter:
    "Units a second over the last `RATE_WINDOW`, of one count."

    def __init__(self, count: Callable[[], int]) -> None:
        self.count = count
        self._samples: deque[tuple[float, int]] = deque()
        #: The rate at the last sample.
        self.rate = 0.0

    def sample(self, now: float) -> None:
        samples = self._samples
        if samples and now < samples[-1][0]:
            # Another clock: a test's. Nothing before it compares.
            samples.clear()
        samples.append((now, self.count()))
        while len(samples) > 1 and now - samples[1][0] >= RATE_WINDOW:
            samples.popleft()
        (then, before), (_now, total) = samples[0], samples[-1]
        self.rate = (total - before) / (now - then) if now > then else 0.0


class Meters:
    "Every meter of the server, and the clock that samples them."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # Each meter belongs to a connection, and a connection is not kept.
        "_meters": Keep.DROPPED,
        "clock": Keep.REBUILT,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._meters: weakref.WeakSet[Meter] = weakref.WeakSet()
        #: What `sample` takes the time from; a test samples by hand.
        self.clock: Callable[[], float] = time.monotonic

    def meter(self, count: Callable[[], int]) -> Meter:
        "A meter of `count`, sampled with the others while its owner lives."
        meter = Meter(count)
        self._meters.add(meter)
        return meter

    def sample(self, now: float | None = None) -> None:
        "Sample every meter, then redraw each client whose readings moved."
        now = self.clock() if now is None else now
        for meter in list(self._meters):
            meter.sample(now)
        for client_state in self.pymux._client_states.values():
            app = client_state.app
            if any(read() != text for text, read in app.meters_shown.values()):
                app.invalidate_for_time()

    async def run(self) -> None:
        "Sample every `SAMPLE_SECONDS` until the server stops."
        while True:
            await anyio.sleep(SAMPLE_SECONDS)
            try:
                self.sample()
            except Exception:
                # A meter must not take the server with it: the siblings
                # of this task are every client.
                logger.exception("Sampling the meters failed.")
