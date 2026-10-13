"""
The client's half of sound: play what the panes play, and lend the
microphone while a program on the server records. Lillecarl/pymux#657.

The sound goes on a connection of its own to the server's socket, which
over SSH is a channel of its own. The round trip is measured on that
connection, so it holds what queues behind other traffic in the same
TCP stream, and the speaker's buffer is that round trip times
`audio-rtt-multiplier`.

The sound card is behind `Devices`, so a test can stand in for it.
`sounddevice` (PortAudio) is the real one, on macOS and Linux alike.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol

import anyio
import anyio.abc
from libpymux.protocol import READ_SIZE, Field, Packet
from libpymux.wire import MSGPACK, PacketReader, encode_json, encode_msgpack

from pymux.enums import ClientAudio
from pymux.jitter import FRAME_MS, JitterBuffer

try:
    import opuslib
except ImportError:
    opuslib = None

__all__ = [
    "AudioClient",
    "Devices",
    "Link",
    "RoundTrip",
    "sound_devices",
]

logger = logging.getLogger(__name__)

RATE = 48000
FRAME = RATE * FRAME_MS // 1000
MICROPHONE_CHANNELS = 1
MICROPHONE_BITRATE = 64000

#: The least a speaker buffers, however short the round trip: two
#: frames, so one late packet on a local link is not a gap.
FLOOR_MS = 2 * FRAME_MS
#: And the most, so a link that stalled once does not keep a second of
#: delay for ever.
CEILING_MS = 1000

#: How often the round trip is measured, and how many measurements the
#: buffer follows. The largest of them sizes it: a buffer has to hold
#: the slow round trips, not the typical one.
ECHO_EVERY = 1.0
FIRST_ECHOES = 5
FIRST_ECHO_EVERY = 0.2
ROUND_TRIPS = 10

#: How long the speaker stays open with nothing to play. Closing it
#: lets the machine sleep and other programs take the device.
SPEAKER_IDLE = 2.0


class RoundTrip:
    "The recent round trips of an audio connection, in seconds."

    def __init__(self) -> None:
        self._recent: deque[float] = deque(maxlen=ROUND_TRIPS)

    def heard(self, seconds: float) -> None:
        if seconds >= 0:
            self._recent.append(seconds)

    def __len__(self) -> int:
        return len(self._recent)

    def buffer_ms(self, multiplier: float) -> float:
        "What a speaker should hold for these round trips."
        if not self._recent:
            return FLOOR_MS
        wanted = max(self._recent) * 1000 * multiplier
        return min(CEILING_MS, max(FLOOR_MS, wanted))


class Stream(Protocol):
    def close(self) -> None: ...


class Devices(Protocol):
    """
    The machine's sound card. Each callback runs on a thread the device
    owns, once per 20 ms frame of 16-bit native samples at 48 kHz.
    """

    def speaker(self, channels: int, pull: Callable[[], bytes | None]) -> Stream: ...

    def microphone(self, channels: int, push: Callable[[bytes], None]) -> Stream: ...


class Link(Protocol):
    "One byte stream to the server's socket."

    async def receive(self) -> bytes: ...

    async def send(self, data: bytes) -> None: ...

    async def aclose(self) -> None: ...


class AudioClient:
    def __init__(self, client_id: str, devices: Devices) -> None:
        self.client_id = client_id
        self.devices = devices
        self.mode = ClientAudio.ON
        self.multiplier = 2.0
        self.round_trip = RoundTrip()
        self.speaker_buffer = JitterBuffer(FLOOR_MS)
        self._told_ms: int | None = None
        self._speaker: Stream | None = None
        self._speaker_channels = 0
        self._decoder = None
        self._last_sound = 0.0
        self._microphone: Stream | None = None
        self._heard: deque[bytes] = deque(maxlen=50)
        self._heard_lock = threading.Lock()
        self._encoder = None
        self._link: Link | None = None
        self._encode = encode_json
        #: One send at a time: the echoes, the microphone and the
        #: answers to the server all write on one stream.
        self._sending = anyio.Lock()

    async def run(self, open_link: Callable[[], Awaitable[Link]]) -> None:
        "Carry sound on one connection, until it ends."
        link = await open_link()
        self._link = link
        self._encode = encode_json
        try:
            await self._send(
                {
                    Field.CMD: Packet.AUDIO,
                    Field.CLIENT_ID: self.client_id,
                    Field.WIRE: [MSGPACK],
                }
            )
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self._read, link, tasks)
                tasks.start_soon(self._echo)
                tasks.start_soon(self._idle)
        finally:
            self._close_speaker()
            self._close_microphone()
            self._link = None
            with anyio.CancelScope(shield=True):
                await link.aclose()

    async def _send(self, packet: dict) -> None:
        async with self._sending:
            if self._link is not None:
                await self._link.send(self._encode(packet))

    async def _read(self, link: Link, tasks: anyio.abc.TaskGroup) -> None:
        reader = PacketReader()
        while True:
            try:
                data = await link.receive()
            except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
                data = b""
            if not data:
                tasks.cancel_scope.cancel()
                return
            for packet in reader.feed(data):
                await self._process(packet, tasks)

    async def _process(self, packet: dict, tasks: anyio.abc.TaskGroup) -> None:
        kind = packet.get(Field.CMD)
        if kind == Packet.WIRE:
            # The server writes msgpack from here on. This side turns
            # too, saying so in the last JSON packet it writes.
            async with self._sending:
                if self._link is not None:
                    await self._link.send(self._encode({Field.CMD: Packet.WIRE, Field.WIRE: MSGPACK}))
                self._encode = encode_msgpack
        elif kind == Packet.SOUND:
            self._play(packet)
        elif kind == Packet.ECHO:
            sent = packet.get(Field.TIME)
            if isinstance(sent, (int, float)):
                self.round_trip.heard(anyio.current_time() - sent)
                await self._retarget()
        elif kind == Packet.AUDIO:
            try:
                self.mode = ClientAudio(packet.get(Field.DATA))
            except ValueError:
                pass
            multiplier = packet.get(Field.RTT_MULTIPLIER)
            if isinstance(multiplier, (int, float)) and multiplier > 0:
                self.multiplier = float(multiplier)
            if self.mode == ClientAudio.OFF:
                self._close_speaker()
            if self.mode != ClientAudio.ON:
                self._close_microphone()
            await self._retarget()
        elif kind == Packet.MICROPHONE:
            if packet.get(Field.DATA) and self.mode == ClientAudio.ON:
                self._open_microphone(tasks)
            else:
                self._close_microphone()

    async def _retarget(self) -> None:
        "Size the speaker's buffer, and tell the server, which sizes the microphone's."
        wanted = self.round_trip.buffer_ms(self.multiplier)
        self.speaker_buffer.retarget(wanted)
        told = self.speaker_buffer.target_ms
        if told != self._told_ms:
            self._told_ms = told
            await self._send({Field.CMD: Packet.LATENCY, Field.BUFFER: told})

    async def _echo(self) -> None:
        for _ in range(FIRST_ECHOES):
            await self._send({Field.CMD: Packet.ECHO, Field.TIME: anyio.current_time()})
            await anyio.sleep(FIRST_ECHO_EVERY)
        while True:
            await self._send({Field.CMD: Packet.ECHO, Field.TIME: anyio.current_time()})
            await anyio.sleep(ECHO_EVERY)

    # ------------------------------------------------------------------
    # The speaker.

    def _play(self, packet: dict) -> None:
        data = packet.get(Field.DATA)
        channels = packet.get(Field.CHANNELS)
        if self.mode == ClientAudio.OFF or opuslib is None or not isinstance(data, bytes):
            return
        if not isinstance(channels, int) or not 1 <= channels <= 2:
            return
        if channels != self._speaker_channels:
            self._close_speaker()
            self._decoder = opuslib.Decoder(RATE, channels)
            self._speaker_channels = channels
        try:
            frame = self._decoder.decode(data, FRAME)
        except opuslib.OpusError:
            return
        self.speaker_buffer.put(frame)
        self._last_sound = anyio.current_time()
        if self._speaker is None:
            try:
                self._speaker = self.devices.speaker(channels, self.speaker_buffer.take)
            except Exception as error:
                logger.warning("The speaker did not open: %s", error)
                self.mode = ClientAudio.OFF

    async def _idle(self) -> None:
        while True:
            await anyio.sleep(SPEAKER_IDLE / 2)
            if self._speaker is not None and anyio.current_time() - self._last_sound > SPEAKER_IDLE:
                self._close_speaker()

    def _close_speaker(self) -> None:
        if self._speaker is not None:
            self._speaker.close()
            self._speaker = None
        self._speaker_channels = 0
        self.speaker_buffer.clear()

    # ------------------------------------------------------------------
    # The microphone.

    def _open_microphone(self, tasks: anyio.abc.TaskGroup) -> None:
        if self._microphone is not None or opuslib is None:
            return
        self._encoder = opuslib.Encoder(RATE, MICROPHONE_CHANNELS, opuslib.APPLICATION_VOIP)
        self._encoder.bitrate = MICROPHONE_BITRATE
        try:
            self._microphone = self.devices.microphone(MICROPHONE_CHANNELS, self._hear)
        except Exception as error:
            logger.warning("The microphone did not open: %s", error)
            return
        tasks.start_soon(self._send_microphone, self._microphone)

    def _hear(self, frame: bytes) -> None:
        with self._heard_lock:
            self._heard.append(frame)

    async def _send_microphone(self, microphone: Stream) -> None:
        "What the microphone heard, every 20 ms, while it stays open."
        while self._microphone is microphone:
            with self._heard_lock:
                frames = list(self._heard)
                self._heard.clear()
            for frame in frames:
                await self._send({Field.CMD: Packet.SOUND, Field.DATA: self._encoder.encode(frame, FRAME)})
            await anyio.sleep(FRAME_MS / 1000)

    def _close_microphone(self) -> None:
        if self._microphone is not None:
            self._microphone.close()
            self._microphone = None
        with self._heard_lock:
            self._heard.clear()


# ----------------------------------------------------------------------
# Transports.


class SocketLink:
    "A link over anyio's socket stream: the unix socket of a local server."

    def __init__(self, stream: anyio.abc.ByteStream) -> None:
        self.stream = stream

    async def receive(self) -> bytes:
        return await self.stream.receive(READ_SIZE)

    async def send(self, data: bytes) -> None:
        await self.stream.send(data)

    async def aclose(self) -> None:
        await self.stream.aclose()


class SshLink:
    "A link over an asyncssh channel to the socket on the far side."

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer

    async def receive(self) -> bytes:
        return await self.reader.read(READ_SIZE)

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def aclose(self) -> None:
        self.writer.close()


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
            self.sd.RawInputStream(samplerate=RATE, channels=channels, dtype="int16", blocksize=FRAME, callback=callback)
        )


def sound_devices() -> Devices | None:
    "This machine's sound card, or None where sound cannot run."
    if opuslib is None:
        return None
    try:
        import sounddevice
    except (ImportError, OSError):
        # OSError: the module loads PortAudio when it is imported.
        return None
    return SoundDevices(sounddevice)

