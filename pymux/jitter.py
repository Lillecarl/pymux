"""
The buffer in front of a speaker that sound reaches over a network.

Sound arrives in bursts and leaves at the speaker's clock. The buffer
holds `target` frames before it plays, so a late burst does not stop
the sound. Its size is the round trip times a multiplier, which the
client measures on the audio connection itself (`client/audio.py`).
Lillecarl/pymux#657.

The link is TCP, so nothing is lost or reordered: a frame is late or on
time. That keeps this a queue and not a reorder buffer.

**Thread-safe**, because a speaker's callback runs on a thread of the
sound library, and frames arrive on the event loop.
"""

from __future__ import annotations

import math
import threading
from collections import deque

__all__ = [
    "FRAME_MS",
    "JitterBuffer",
    "RoundTrip",
    "frames_for",
]

#: One Opus frame, and one packet of PipeWire's RTP on loopback.
FRAME_MS = 20
#: Its samples, per channel, at the 48 kHz everything here runs at.
SAMPLES_PER_FRAME = 48000 * FRAME_MS // 1000

#: The takes the average fill is taken over: a second of frames.
AVERAGE_OVER = 1000 // FRAME_MS

#: Frames held past the target before the oldest are dropped. A burst
#: after a stall would otherwise keep its delay for ever, because the
#: speaker drains only at its own pace.
SLACK = 3


#: The least a buffer holds, however short the round trip: two frames,
#: so one late packet on a local link is not a gap, and short enough to
#: talk over. Fixed, not an option: Lillecarl/pymux#661.
FLOOR_MS = 2 * FRAME_MS
#: And the most, so a link that stalled once does not keep a second of
#: delay for ever.
CEILING_MS = 1000

#: How often the server measures the round trip, and how many
#: measurements the buffers follow. The largest of them sizes them: a
#: buffer has to hold the slow round trips, not the typical one.
ECHO_EVERY = 1.0
FIRST_ECHOES = 5
FIRST_ECHO_EVERY = 0.2
ROUND_TRIPS = 10


class RoundTrip:
    "The recent round trips of a connection, in seconds."

    def __init__(self) -> None:
        self._recent: deque[float] = deque(maxlen=ROUND_TRIPS)

    def heard(self, seconds: float) -> None:
        if seconds >= 0:
            self._recent.append(seconds)

    def __len__(self) -> int:
        return len(self._recent)

    def buffer_ms(self, multiplier: float) -> float:
        "What a buffer should hold for these round trips."
        if not self._recent:
            return FLOOR_MS
        wanted = max(self._recent) * 1000 * multiplier
        return min(CEILING_MS, max(FLOOR_MS, wanted))


def frames_for(milliseconds: float) -> int:
    "The frames that hold this much sound, at least one."
    return max(1, math.ceil(milliseconds / FRAME_MS))


class JitterBuffer:
    """
    Sound held between the network and a speaker, as 16-bit samples.

    **It follows the drift of two clocks.** The far end makes sound at
    its own clock and the speaker drains at its own, and no two clocks
    agree: by a hundred parts per million a buffer of any size fills or
    empties in minutes. So a frame taken while the buffer runs full is
    made of one sample more than it plays, with one dropped, and one
    taken while it runs empty repeats one. That is up to a thousand
    parts per million either way, ten times the usual drift, and a
    single sample in 960 is not heard. It is how other players keep two
    clocks together without a resampler. Lillecarl/pymux#660.

    It judges the fill on an average of about a second, so a burst off
    the network moves nothing: only a fill that stays off its target is
    drift.
    """

    def __init__(self, target_ms: float = FLOOR_MS, channels: int = 1) -> None:
        self._lock = threading.Lock()
        self._held = bytearray()
        self._target = frames_for(target_ms)
        self.channels = channels
        #: Whether it plays, or fills up to the target first.
        self._playing = False
        #: The fill an average of the last second of takes saw, in frames.
        self._average: float | None = None
        self.underruns = 0
        self.dropped = 0
        #: Samples dropped and repeated to follow the other clock.
        self.squeezed = 0
        self.stretched = 0

    @property
    def target_ms(self) -> int:
        return self._target * FRAME_MS

    @property
    def _stride(self) -> int:
        return 2 * self.channels

    @property
    def _frame(self) -> int:
        return SAMPLES_PER_FRAME * self._stride

    def retarget(self, target_ms: float) -> None:
        "Hold this much from the next fill on. A smaller target drops what is over it."
        with self._lock:
            self._target = frames_for(target_ms)
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
            self._average = fill if self._average is None else self._average + (fill - self._average) / AVERAGE_OVER
            if len(self._held) < frame - stride:
                # Out of sound: fill to the target again before playing,
                # rather than stutter one frame at a time.
                self._playing = False
                self.underruns += 1
                return None
            middle = (frame // 2) // stride * stride
            if self._average > self._target + 1 and len(self._held) >= frame + stride:
                taken = bytes(self._held[: frame + stride])
                out = taken[:middle] + taken[middle + stride :]
                self.squeezed += 1
            elif self._average < self._target or len(self._held) < frame:
                taken = bytes(self._held[: frame - stride])
                out = taken[: middle + stride] + taken[middle:]
                self.stretched += 1
            else:
                taken = out = bytes(self._held[:frame])
            del self._held[: len(taken)]
            return out

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
        while len(self._held) > (self._target + SLACK) * frame:
            del self._held[:frame]
            self.dropped += 1
