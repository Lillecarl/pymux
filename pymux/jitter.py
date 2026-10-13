"""
The buffer in front of a speaker that sound reaches over a network.

Sound arrives in bursts and leaves at the speaker's clock. The buffer
holds `target` frames before it plays, so a late burst does not stop
the sound. Its size is the round trip times a multiplier, which the
server measures on the client's connection (`audio.py`).
Lillecarl/pymux#657.

A frame is any size Opus encodes, which each client chooses
(`audio-frame`). So every tuning here is in milliseconds, and turns into
frames of the size at hand.

The link is TCP, so nothing is lost or reordered: a frame is late or on
time. That keeps this a queue and not a reorder buffer.

**Thread-safe**, because a speaker's callback runs on a thread of the
sound library, and frames arrive on the event loop.
"""

from __future__ import annotations

import math
import os
import threading
from collections import deque

__all__ = [
    "DEFAULT_FRAME_MS",
    "FRAME_SIZES",
    "JitterBuffer",
    "RoundTrip",
    "floor_ms",
    "samples_per_frame",
]

RATE = 48000

#: The frame sizes Opus encodes, in milliseconds, and the one a client
#: plays unless it chooses: short enough that two of them make a 20 ms
#: buffer, long enough for 100 packets a second each way.
FRAME_SIZES = (2.5, 5, 10, 20, 40, 60)
DEFAULT_FRAME_MS = 10

#: How long the average fill is taken over.
AVERAGE_MS = 1000

#: Sound held past the target before the oldest frames are dropped. A
#: burst after a stall would otherwise keep its delay for ever, because
#: the speaker drains only at its own pace. Past the dead band and the
#: ramp below, so drift that is being followed never reaches it.
SLACK_MS = 100
#: And never fewer frames than this: at 60 ms frames 100 ms is two, and
#: one burst past the steady fill reached it.
SLACK_FRAMES = 3

#: How far off its steady fill the buffer may sit before the drift is
#: followed, and over how much more the share of frames corrected grows
#: to all of them. Tighter below: a buffer that runs low is a gap away,
#: and one that runs high only waits longer.
DEAD_BAND_MS = 30
RAMP_MS = 20
DEAD_BAND_LOW_MS = 10
RAMP_LOW_MS = 10

#: The least a buffer holds, however short the round trip: two frames,
#: so one late packet on a local link is not a gap. Lillecarl/pymux#661.
FLOOR_FRAMES = 2
#: And the most, so a link that stalled once does not keep a second of
#: delay for ever.
CEILING_MS = 1000

#: How often the server measures the round trip, and how many
#: measurements the buffers follow.
ECHO_EVERY = 1.0
FIRST_ECHOES = 5
FIRST_ECHO_EVERY = 0.2
ROUND_TRIPS = 10


def samples_per_frame(frame_ms: float) -> int:
    "A frame's samples, per channel, at the 48 kHz everything here runs at."
    return round(RATE * frame_ms / 1000)


def floor_ms(frame_ms: float) -> float:
    return FLOOR_FRAMES * frame_ms


class RoundTrip:
    "The recent round trips of a connection, in seconds."

    def __init__(self) -> None:
        self._recent: deque[float] = deque(maxlen=ROUND_TRIPS)

    def heard(self, seconds: float) -> None:
        if seconds >= 0:
            self._recent.append(seconds)

    def __len__(self) -> int:
        return len(self._recent)

    def describe(self) -> str:
        "The recent round trips for a log line: fastest, median and slowest, in ms."
        if not self._recent:
            return "unmeasured"
        ordered = sorted(self._recent)
        return "%.1f/%.1f/%.1f" % (ordered[0] * 1000, ordered[len(ordered) // 2] * 1000, ordered[-1] * 1000)

    def buffer_ms(self, multiplier: float, floor: float) -> float:
        "What a buffer should hold for these round trips: the slow ones, but not one alone."
        if not self._recent:
            return floor
        # The second slowest: one slow echo, a client busy drawing for a
        # moment, made a buffer six times its size for ten seconds.
        ordered = sorted(self._recent)
        wanted = ordered[-2 if len(ordered) > 2 else -1] * 1000 * multiplier
        return min(CEILING_MS, max(floor, wanted))


def stats_every() -> float | None:
    """
    How often both ends log the state of their sound, in seconds:
    `PYMUX_AUDIO_STATS`, for measuring a real link. Unset, never.
    """
    try:
        seconds = float(os.environ.get("PYMUX_AUDIO_STATS", ""))
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class JitterBuffer:
    """
    Sound held between the network and a speaker, as 16-bit samples.

    **It follows the drift of two clocks.** The far end makes sound at
    its own clock and the speaker drains at its own, and no two clocks
    agree: by a hundred parts per million a buffer of any size fills or
    empties in minutes. So a frame taken while the buffer runs full is
    made of one sample more than it plays, with one dropped, and one
    taken while it runs empty repeats one. That is one sample a frame:
    from 350 parts per million at 60 ms frames to 8000 at 2.5 ms, past
    the usual drift of under a hundred, and not heard. It is how other
    players keep two clocks together without a resampler.
    Lillecarl/pymux#660.

    It judges the fill on an average of about a second, so a burst off
    the network moves nothing: only a fill that stays off its target is
    drift.
    """

    def __init__(self, target_ms: float | None = None, channels: int = 1, frame_ms: float = DEFAULT_FRAME_MS) -> None:
        self._lock = threading.Lock()
        self._held = bytearray()
        self.frame_ms = frame_ms
        self._target = self._frames_for(floor_ms(frame_ms) if target_ms is None else target_ms)
        self.channels = channels
        #: Whether it plays, or fills up to the target first.
        self._playing = False
        #: The fill an average of the last second of takes saw, in frames.
        self._average: float | None = None
        #: Corrections owed, in frames: a share of one is added per take.
        self._owed = 0.0
        self.underruns = 0
        self.dropped = 0
        #: Samples dropped and repeated to follow the other clock.
        self.squeezed = 0
        self.stretched = 0

    @property
    def target_ms(self) -> float:
        return self._target * self.frame_ms

    def _frames_for(self, milliseconds: float) -> int:
        "The frames that hold this much sound, at least one."
        return max(1, math.ceil(milliseconds / self.frame_ms - 1e-9))

    def reframe(self, frame_ms: float) -> None:
        "Hold frames of this size from now on. What it held goes: it was cut to the old size."
        with self._lock:
            target_ms = self.target_ms
            self.frame_ms = frame_ms
            self._target = self._frames_for(max(target_ms, floor_ms(frame_ms)))
            self._held.clear()
            self._playing = False
            self._average = None

    @property
    def _stride(self) -> int:
        return 2 * self.channels

    @property
    def _frame(self) -> int:
        return samples_per_frame(self.frame_ms) * self._stride

    def retarget(self, target_ms: float) -> None:
        "Hold this much from the next fill on. A smaller target drops what is over it."
        with self._lock:
            self._target = self._frames_for(target_ms)
            self._trim()

    def put(self, frame: bytes) -> None:
        with self._lock:
            self._held += frame
            if not self._playing and len(self._held) >= self._target * self._frame:
                self._playing = True
                self._average = None
            self._trim()

    def take(self) -> bytes | None:
        "The next frame to play, always one frame long, or None while it fills."
        with self._lock:
            if not self._playing:
                return None
            frame, stride = self._frame, self._stride
            fill = len(self._held) / frame
            over = AVERAGE_MS / self.frame_ms
            self._average = fill if self._average is None else self._average + (fill - self._average) / over
            if len(self._held) < frame - stride:
                # Out of sound: fill to the target again before playing,
                # rather than stutter one frame at a time.
                self._playing = False
                self.underruns += 1
                return None
            middle = (frame // 2) // stride * stride
            # A steady buffer sits half a frame over its target before a
            # take. Inside the dead band nothing is corrected: frames off
            # a real network arrive unevenly, and a run from the MacBook
            # sat a frame and a quarter over, squeezing every frame.
            # Past it, a share of frames that grows with the error.
            error = (self._average - (self._target + 0.5)) * self.frame_ms
            band, ramp = (DEAD_BAND_MS, RAMP_MS) if error > 0 else (DEAD_BAND_LOW_MS, RAMP_LOW_MS)
            self._owed += max(0.0, min(1.0, (abs(error) - band) / ramp))
            correct = 0
            if self._owed >= 1:
                self._owed -= 1
                correct = 1 if error > 0 else -1
            if correct > 0 and len(self._held) >= frame + stride:
                taken = bytes(self._held[: frame + stride])
                out = taken[:middle] + taken[middle + stride :]
                self.squeezed += 1
            elif correct < 0 or len(self._held) < frame:
                taken = bytes(self._held[: frame - stride])
                out = taken[: middle + stride] + taken[middle:]
                self.stretched += 1
            else:
                taken = out = bytes(self._held[:frame])
            del self._held[: len(taken)]
            return out

    def describe(self) -> str:
        "Its state, for a stats line."
        with self._lock:
            return "frames of %g ms, holds %d/%g ms, underruns %d, dropped %d, squeezed %d, stretched %d" % (
                self.frame_ms,
                len(self._held) // self._stride * 1000 // RATE,
                self.target_ms,
                self.underruns,
                self.dropped,
                self.squeezed,
                self.stretched,
            )

    def clear(self) -> None:
        with self._lock:
            self._held.clear()
            self._playing = False
            self._average = None

    def __len__(self) -> int:
        "Whole frames held."
        with self._lock:
            return len(self._held) // self._frame

    def _trim(self) -> None:
        # A burst after a stall would otherwise keep its delay for ever:
        # the drift correction moves a sample a frame, a stall seconds.
        frame = self._frame
        slack = max(SLACK_FRAMES, math.ceil(SLACK_MS / self.frame_ms))
        while len(self._held) > (self._target + slack) * frame:
            del self._held[:frame]
            self.dropped += 1
