"""
The client's half of sound: play what the panes play, and lend the
microphone while a program on the server records. Lillecarl/pymux#657.

It rides the connection the client attached on: the server writes sound
in an urgent lane there (`pymux/audio.py` says why), measures the round
trip, and tells this side how much its speaker holds.

The sound card is behind `Devices`, so a test can stand in for it. On
Linux `pw-cat` drives the person's PipeWire; elsewhere `sounddevice`
(PortAudio, CoreAudio on macOS) does.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from libpymux.protocol import Field, Packet

from pymux.audio import programs
from pymux.enums import ClientAudio
from pymux.jitter import FLOOR_MS, FRAME_MS, JitterBuffer

try:
    import opuslib
except ImportError:
    opuslib = None

if TYPE_CHECKING:
    from opuslib import Decoder

__all__ = [
    "AudioClient",
    "Devices",
    "sound_devices",
]

logger = logging.getLogger(__name__)

RATE = 48000
FRAME = RATE * FRAME_MS // 1000
MICROPHONE_CHANNELS = 1
MICROPHONE_BITRATE = 64000

#: `fcntl`'s name for a pipe's size, which Python names from 3.10 on
#: Linux only.
F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", 1031)

#: Frames of exact zeros, a second, after which the microphone is
#: taken for refused.
SILENT_FRAMES = 1000 // FRAME_MS

#: How long the speaker stays open with nothing to play. Closing it
#: lets the machine sleep and other programs take the device.
SPEAKER_IDLE = 2.0


class Stream(Protocol):
    def close(self) -> None: ...


class Devices(Protocol):
    """
    The machine's sound card. Each callback runs on a thread the device
    owns, once per 20 ms frame of 16-bit native samples at 48 kHz.
    """

    def speaker(self, channels: int, pull: Callable[[], bytes | None]) -> Stream: ...

    def microphone(self, channels: int, push: Callable[[bytes], None]) -> Stream: ...


class AudioClient:
    """
    The sound of one attached client.

    Packets come in through `heard`, on whatever reads the connection.
    The speaker pulls on the device's thread, and the microphone is
    encoded and sent on a thread of its own, so `send` has to be safe to
    call from any thread.
    """

    def __init__(self, devices: Devices, send: Callable[[dict], None]) -> None:
        self.devices = devices
        self._send = send
        self.mode = ClientAudio.ON
        self.speaker_buffer = JitterBuffer(FLOOR_MS)
        self._lock = threading.Lock()
        self._speaker: Stream | None = None
        self._speaker_channels = 0
        self._decoder: Decoder | None = None
        self._last_sound = 0.0
        self._microphone: _Microphone | None = None

    def heard(self, packet: dict) -> None:
        kind = packet.get(Field.CMD)
        if kind == Packet.SOUND:
            self._play(packet)
        elif kind == Packet.LATENCY:
            buffer = packet.get(Field.BUFFER)
            if isinstance(buffer, (int, float)) and buffer > 0:
                self.speaker_buffer.retarget(buffer)
        elif kind == Packet.AUDIO:
            with contextlib.suppress(ValueError):
                self.mode = ClientAudio(packet.get(Field.DATA))
            if self.mode == ClientAudio.OFF:
                self._close_speaker()
            if self.mode != ClientAudio.ON:
                self._close_microphone()
        elif kind == Packet.MICROPHONE:
            if packet.get(Field.DATA) and self.mode == ClientAudio.ON:
                self._open_microphone()
            else:
                self._close_microphone()

    def close(self) -> None:
        "The attachment ended: let both devices go."
        self._close_speaker()
        self._close_microphone()

    # ------------------------------------------------------------------
    # The speaker.

    def _play(self, packet: dict) -> None:
        data = packet.get(Field.DATA)
        channels = packet.get(Field.CHANNELS)
        if self.mode == ClientAudio.OFF or opuslib is None or not isinstance(data, bytes):
            return
        if not isinstance(channels, int) or not 1 <= channels <= 2:
            return
        decoder = self._decoder
        if decoder is None or channels != self._speaker_channels:
            self._close_speaker()
            decoder = self._decoder = opuslib.Decoder(RATE, channels)
            self._speaker_channels = channels
            self.speaker_buffer.channels = channels
        try:
            frame = decoder.decode(data, FRAME)
        except opuslib.OpusError:
            return
        self.speaker_buffer.put(frame)
        self._last_sound = time.monotonic()
        with self._lock:
            if self._speaker is not None:
                return
            try:
                self._speaker = self.devices.speaker(channels, self.speaker_buffer.take)
            except Exception as error:
                logger.warning("The speaker did not open: %s", error)
                self.mode = ClientAudio.OFF
                return
            speaker = self._speaker
        threading.Thread(target=self._close_when_idle, args=(speaker,), name="pymux-speaker", daemon=True).start()

    def _close_when_idle(self, speaker: Stream) -> None:
        while True:
            time.sleep(SPEAKER_IDLE / 2)
            if self._speaker is not speaker:
                return
            if time.monotonic() - self._last_sound > SPEAKER_IDLE:
                with self._lock:
                    if self._speaker is speaker:
                        self._shut_speaker()
                return

    def _close_speaker(self) -> None:
        with self._lock:
            self._shut_speaker()

    def _shut_speaker(self) -> None:
        if self._speaker is not None:
            self._speaker.close()
            self._speaker = None
        self._speaker_channels = 0
        self._decoder = None
        self.speaker_buffer.clear()

    # ------------------------------------------------------------------
    # The microphone.

    def _open_microphone(self) -> None:
        if self._microphone is None and opuslib is not None:
            self._microphone = _Microphone(self.devices, self._send)

    def _close_microphone(self) -> None:
        microphone, self._microphone = self._microphone, None
        if microphone is not None:
            microphone.close()


class _Microphone:
    """
    The open microphone: the device hands frames over on its thread,
    and a thread of this one encodes and sends them.

    **A second of exact zeros is not a quiet room.** A real microphone
    always hears some noise. Exact zeros come from an input volume at
    zero, or from macOS refusing the terminal, which it does with zeros
    and no error. The server is told once, and says so to the person.
    """

    def __init__(self, devices: Devices, send: Callable[[dict], None]) -> None:
        assert opuslib is not None
        self._send = send
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self._encoder = opuslib.Encoder(RATE, MICROPHONE_CHANNELS, opuslib.APPLICATION_VOIP)
        self._encoder.bitrate = MICROPHONE_BITRATE
        self._thread = threading.Thread(target=self._run, name="pymux-microphone", daemon=True)
        self._thread.start()
        try:
            self._stream: Stream | None = devices.microphone(MICROPHONE_CHANNELS, self._hear)
        except Exception as error:
            logger.warning("The microphone did not open: %s", error)
            self._stream = None
            self._stop.set()

    def _hear(self, frame: bytes) -> None:
        with contextlib.suppress(queue.Full):
            self._frames.put_nowait(frame)

    def _run(self) -> None:
        silent = 0
        while not self._stop.is_set():
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue
            silent = silent + 1 if frame.count(0) == len(frame) else 0
            if silent == SILENT_FRAMES:
                self._send({Field.CMD: Packet.MICROPHONE, Field.ERROR: "silent"})
            self._send({Field.CMD: Packet.SOUND, Field.DATA: self._encoder.encode(frame, FRAME)})

    def close(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.close()
        self._thread.join(timeout=2)


# ----------------------------------------------------------------------
# The real sound card.


class _SoundDeviceStream:
    def __init__(self, stream) -> None:
        self.stream = stream
        stream.start()

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()


class SoundDevices:
    "PortAudio through `sounddevice`: CoreAudio on macOS, ALSA or PipeWire on Linux."

    def __init__(self, sounddevice) -> None:
        self.sd = sounddevice

    def speaker(self, channels: int, pull: Callable[[], bytes | None]) -> Stream:
        silence = bytes(FRAME * channels * 2)

        def callback(outdata, frames, _time, _status) -> None:
            frame = pull() if frames == FRAME else None
            outdata[:] = frame if frame is not None else silence[: len(outdata)]

        return _SoundDeviceStream(
            self.sd.RawOutputStream(
                samplerate=RATE, channels=channels, dtype="int16", blocksize=FRAME, callback=callback
            )
        )

    def microphone(self, channels: int, push: Callable[[bytes], None]) -> Stream:
        def callback(indata, frames, _time, _status) -> None:
            if frames == FRAME:
                push(bytes(indata))

        return _SoundDeviceStream(
            self.sd.RawInputStream(
                samplerate=RATE, channels=channels, dtype="int16", blocksize=FRAME, callback=callback
            )
        )


class _PwCatStream:
    """
    One `pw-cat`, fed or read 20 ms at a time on a thread of its own,
    which starts the program too: nothing here blocks the event loop.
    """

    def __init__(self, argv: list[str], environment: dict[str, str] | None, feed) -> None:
        self._argv = argv
        self._environment = environment
        self._feed = feed
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="pymux-pw-cat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        playback = "--playback" in self._argv
        with self._lock:
            if self._stop.is_set():
                return
            try:
                self._process = subprocess.Popen(
                    self._argv,
                    env=self._environment,
                    stdin=subprocess.PIPE if playback else subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL if playback else subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                logger.warning("pw-cat did not start: %s", error)
                return
        pipe = self._process.stdin if playback else self._process.stdout
        if pipe is None:
            return
        if playback:
            # The pipe is the buffer between the jitter buffer and
            # PipeWire, so it is kept to one page: 21 ms of stereo. A
            # write then waits for PipeWire to take the frame before, and
            # PipeWire's clock paces the speaker.
            with contextlib.suppress(OSError):
                fcntl.fcntl(pipe.fileno(), F_SETPIPE_SZ, 4096)
        with contextlib.suppress(OSError, ValueError):
            self._feed(pipe, self._stop)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            process = self._process
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        self._thread.join(timeout=2)
        if process is not None:
            # Closed here, after the program: left to the collector, the
            # flush of a playback pipe raised into nothing.
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()


class PipeWireDevices:
    """
    The person's PipeWire through `pw-cat`, on Linux.

    **Not PortAudio there.** nixpkgs' PortAudio speaks ALSA and OSS only,
    so it reaches PipeWire through ALSA's PipeWire plugin, and a nix-built
    alsa-lib looks for that plugin in its own store path: on a desktop
    that is not NixOS it finds no device. `pw-cat` speaks PipeWire's own
    protocol, and the Linux package carries it.
    """

    def __init__(self, pw_cat: str, environment: dict[str, str] | None = None) -> None:
        self.pw_cat = pw_cat
        self.environment = environment

    def _argv(self, mode: str, channels: int) -> list[str]:
        # One frame of latency: pw-cat asks for 100 ms by default.
        return [
            self.pw_cat,
            mode,
            "--raw",
            "--rate",
            str(RATE),
            "--channels",
            str(channels),
            "--format",
            "s16",
            "--latency",
            "%dms" % (FRAME_MS,),
        ]

    def speaker(self, channels: int, pull: Callable[[], bytes | None]) -> Stream:
        silence = bytes(FRAME * channels * 2)

        def feed(pipe, stop: threading.Event) -> None:
            while not stop.is_set():
                frame = pull()
                pipe.write(frame if frame is not None else silence)
                pipe.flush()

        return _PwCatStream([*self._argv("--playback", channels), "-"], self.environment, feed)

    def microphone(self, channels: int, push: Callable[[bytes], None]) -> Stream:
        size = FRAME * channels * 2

        def feed(pipe, stop: threading.Event) -> None:
            while not stop.is_set():
                frame = pipe.read(size)
                if len(frame) < size:
                    return
                push(frame)

        return _PwCatStream([*self._argv("--record", channels), "-"], self.environment, feed)


def pipewire_answers(environment: Mapping[str, str]) -> bool:
    "Whether a PipeWire listens where a client in this environment looks first."
    remote = environment.get("PIPEWIRE_REMOTE") or "pipewire-0"
    if os.path.isabs(remote):
        return os.path.exists(remote)
    for directory in (environment.get("PIPEWIRE_RUNTIME_DIR"), environment.get("XDG_RUNTIME_DIR")):
        if directory and os.path.exists(os.path.join(directory, remote)):
            return True
    return False


def sound_devices() -> Devices | None:
    "This machine's sound card, or None where sound cannot run."
    if opuslib is None:
        return None
    if sys.platform == "linux":
        pw_cat = programs().get("pw-cat")
        if pw_cat and pipewire_answers(os.environ):
            return PipeWireDevices(pw_cat)
    try:
        import sounddevice
    except ImportError, OSError:
        # OSError: the module loads PortAudio when it is imported.
        return None
    return SoundDevices(sounddevice)
