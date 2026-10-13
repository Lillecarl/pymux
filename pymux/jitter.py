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
    "frames_for",
]

#: One Opus frame, and one packet of PipeWire's RTP on loopback.
FRAME_MS = 20

#: Frames held past the target before the oldest are dropped. A burst
#: after a stall would otherwise keep its delay for ever, because the
#: speaker drains only at its own pace.
SLACK = 3


def frames_for(milliseconds: float) -> int:
    "The frames that hold this much sound, at least one."
    return max(1, math.ceil(milliseconds / FRAME_MS))


class JitterBuffer:
    def __init__(self, target_ms: float = 2 * FRAME_MS) -> None:
        self._lock = threading.Lock()
        self._frames: deque[bytes] = deque()
        self._target = frames_for(target_ms)
        #: Whether it plays, or fills up to the target first.
        self._playing = False
        self.underruns = 0
        self.dropped = 0

    @property
    def target_ms(self) -> int:
        return self._target * FRAME_MS

    def retarget(self, target_ms: float) -> None:
        "Hold this much from the next fill on. A smaller target drops what is over it."
        with self._lock:
            self._target = frames_for(target_ms)
            self._trim()

    def put(self, frame: bytes) -> None:
        with self._lock:
            self._frames.append(frame)
            if not self._playing and len(self._frames) >= self._target:
                self._playing = True
            self._trim()

    def take(self) -> bytes | None:
        "The next frame to play, or None while it fills."
        with self._lock:
            if not self._playing:
                return None
            if not self._frames:
                # Out of sound: fill to the target again before playing,
                # rather than stutter one frame at a time.
                self._playing = False
                self.underruns += 1
                return None
            return self._frames.popleft()

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._playing = False

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    def _trim(self) -> None:
        while len(self._frames) > self._target + SLACK:
            self._frames.popleft()
            self.dropped += 1
