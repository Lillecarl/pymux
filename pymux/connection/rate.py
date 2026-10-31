"""
A rate of bytes per second, read off a count that only goes up.

**Sampled when it is read, not on a timer.** The status line reads a
client's rate once each `status-interval`, and each read keeps a
sample. The rate is the bytes since the newest sample at least
`RATE_WINDOW` old, over the seconds since it: a client that went quiet
reads zero a window later, never a falling average over its uptime.
A count that nobody reads costs nothing. Lillecarl/pymux#678.

A rate in a status line moves, so the line redraws each tick on an
idle client, and the bytes of that frame are in the next rate. That is
about 80 bytes a tick, measured by `checks.pymux-wire.run`.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

__all__ = ["RATE_WINDOW", "Rate", "frames_per_second", "per_second"]

#: How far back a rate looks, in seconds: past the 4 s of the default
#: `status-interval`, so a status line always has a sample that old.
RATE_WINDOW = 5.0

#: How often a read keeps a sample, in seconds. A status line reads
#: several times a frame, and one sample a second bounds the queue.
SAMPLE_EVERY = 1.0

#: The units a rate picks from, each a thousand times the one before.
UNITS = ("B/s", "kB/s", "MB/s", "GB/s")


class Rate:
    "Bytes per second over the last `RATE_WINDOW`, of one count."

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int]] = deque()
        #: What `read_now` takes the time from. A test sets its own, so
        #: a rate it checks does not depend on how fast the machine is.
        #: Lillecarl/pymux#844.
        self.clock: Callable[[], float] = time.monotonic

    def read_now(self, total: int) -> float:
        "The rate, now by `clock`, of a count that stands at `total`."
        return self.read(total, self.clock())

    def read(self, total: int, now: float) -> float:
        "The rate, at `now`, of a count that stands at `total`."
        samples = self._samples
        if not samples or now - samples[-1][0] >= SAMPLE_EVERY:
            samples.append((now, total))
        while len(samples) > 1 and now - samples[1][0] >= RATE_WINDOW:
            samples.popleft()
        then, before = samples[0]
        if now <= then:
            return 0.0
        return (total - before) / (now - then)


def frames_per_second(rate: float) -> str:
    "A frame rate as a person reads it: `0.3 fps`, `12 fps`."
    if rate < 9.95:
        return "%.1f fps" % (rate,)
    return "%d fps" % (round(rate),)


def per_second(rate: float) -> str:
    "A rate as a person reads it: `135 B/s`, `1.4 kB/s`, `13 MB/s`."
    step = 0
    # 999.5 and not 1000: it rounds to "1000 B/s" otherwise.
    while rate >= 999.5 and step < len(UNITS) - 1:
        rate /= 1000
        step += 1
    if rate < 9.95 and step:
        return "%.1f %s" % (rate, UNITS[step])
    return "%d %s" % (round(rate), UNITS[step])
