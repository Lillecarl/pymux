"""
Sound between panes and a client, through a real PipeWire. Needs
`pipewire`, `wireplumber` and the PipeWire tools on the PATH, so
`checks.pymux-audio` runs this and `checks.pymux-unit` skips it.
Lillecarl/pymux#657.

The client's sound card is `FakeDevices`: PortAudio has no device in a
build sandbox. Each fake runs on a thread every 20 ms, as a real
callback does.
"""

from __future__ import annotations

import itertools
import math
import os
import shutil
import struct
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager

import anyio
import pytest
from prompt_toolkit.data_structures import Size
from session import once, over_connection

if not (shutil.which("pipewire") and shutil.which("wireplumber") and shutil.which("pw-play")):
    pytest.skip("PipeWire is not on the PATH", allow_module_level=True)
pytest.importorskip("opuslib")

from libpymux.protocol import Field, Packet

from pymux.audio import programs
from pymux.client.audio import AudioClient, PipeWireDevices, SocketLink
from pymux.enums import AudioServerMode
from pymux.main import Pymux

RATE = 48000
FRAME = 960
#: Bytes of one second of 16-bit stereo.
STEREO_SECOND = RATE * 2 * 2


def tone(hz: float, seconds: float, channels: int, start: int = 0) -> bytes:
    samples = []
    for n in range(start, start + int(RATE * seconds)):
        value = int(12000 * math.sin(2 * math.pi * hz * n / RATE))
        samples.extend([value] * channels)
    return struct.pack("<%dh" % len(samples), *samples)


def frequency(pcm: bytes, channels: int) -> float:
    """
    The frequency of a sine, from its rising zero crossings, over the
    frames that are not silent. A gap is a matter of load, which
    `silent_frames` counts; this judges the pitch.
    """
    every = struct.unpack("<%dh" % (len(pcm) // 2), pcm)[::channels]
    samples = [
        sample
        for at in range(0, len(every), FRAME)
        if max(map(abs, every[at : at + FRAME]), default=0) >= 100
        for sample in every[at : at + FRAME]
    ]
    rising = sum(1 for a, b in itertools.pairwise(samples) if a < 0 <= b)
    return rising * RATE / len(samples) if samples else 0.0


def silent_frames(pcm: bytes, channels: int = 1) -> int:
    "How many 20 ms frames of sound are all near zero, in the first channel."
    samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm)[::channels]
    return sum(1 for at in range(0, len(samples), FRAME) if max(map(abs, samples[at : at + FRAME]), default=0) < 100)


class _Ticking:
    def __init__(self, every_frame) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(every_frame,), daemon=True)
        self._thread.start()

    def _run(self, every_frame) -> None:
        deadline = time.monotonic()
        while not self._stop.is_set():
            deadline += FRAME / RATE
            every_frame()
            time.sleep(max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        self._stop.set()
        self._thread.join()


class FakeDevices:
    "A speaker that keeps what it plays, and a microphone that hears a tone."

    def __init__(self, microphone_hz: float = 880.0) -> None:
        self.played = bytearray()
        self.speaker_channels = 0
        self.speakers_opened = 0
        self.microphone_hz = microphone_hz
        self.microphone_open = False
        self.microphones_opened = 0

    def speaker(self, channels, pull):
        self.speaker_channels = channels
        self.speakers_opened += 1

        def every_frame() -> None:
            frame = pull()
            if frame is not None:
                self.played += frame

        return _Ticking(every_frame)

    def microphone(self, channels, push):
        self.microphone_open = True
        self.microphones_opened += 1
        position = [0]

        def every_frame() -> None:
            push(tone(self.microphone_hz, FRAME / RATE, channels, position[0]))
            position[0] += FRAME

        ticking = _Ticking(every_frame)
        devices = self

        class Microphone:
            def close(self) -> None:
                devices.microphone_open = False
                ticking.close()

        return Microphone()


@asynccontextmanager
async def serving(tmp_path, mode=AudioServerMode.PRIVATE, name="pymux.sock"):
    pymux = Pymux()
    pymux.test_mode = True
    pymux.audio_server = mode
    pymux.listen_on_socket(str(tmp_path / name))
    async with pymux.running():
        try:
            with anyio.fail_after(20):
                await pymux.audio.running.wait()
            yield pymux
        finally:
            pymux.stop()


@asynccontextmanager
async def carrying(pymux, devices, client_id="the-client"):
    "A client's audio connection to `pymux`, until the block ends."
    client = AudioClient(client_id, devices)

    async def open_link():
        return SocketLink(await anyio.connect_unix(pymux.socket_name))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(client.run, open_link)
        await once(lambda: client_id in pymux.audio.links, 10.0, "the audio connection never arrived")
        try:
            yield client
        finally:
            tasks.cancel_scope.cancel()


@asynccontextmanager
async def running(command: list[str], environment: dict[str, str]):
    """
    A program for the length of the block, killed at its end. A failure
    inside the block must not wait for a recorder that never ends.
    """
    process = await anyio.open_process(command, env=environment)
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            await process.wait()


def pane_environment(pymux) -> dict[str, str]:
    environment = dict(os.environ)
    pymux.audio.pane_environment(environment)
    return environment


def write_wav(path, pcm: bytes, channels: int) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)


async def test_a_pane_plays_on_the_client(tmp_path):
    "A tone played on the server comes out of the client's speaker, as the same tone."
    devices = FakeDevices()
    async with serving(tmp_path) as pymux, carrying(pymux, devices):
        song = tmp_path / "a440.wav"
        write_wav(song, tone(440, 3.0, 2), 2)
        async with running(["pw-play", str(song)], pane_environment(pymux)):
            await once(
                lambda: len(devices.played) >= 2.5 * STEREO_SECOND,
                15.0,
                lambda: "played %d bytes" % len(devices.played),
            )

    assert devices.speaker_channels == 2
    # The middle second: the start and the end are a fade.
    # The second second: the first holds the buffers filling.
    middle = bytes(devices.played[STEREO_SECOND : 2 * STEREO_SECOND])
    assert abs(frequency(middle, 2) - 440) < 5
    assert silent_frames(middle, 2) <= 3


async def test_the_microphone_opens_only_while_a_program_records(tmp_path):
    devices = FakeDevices(microphone_hz=880)
    async with serving(tmp_path) as pymux, carrying(pymux, devices):
        # Nobody records: the person's microphone stays shut.
        await anyio.sleep(1.0)
        assert devices.microphones_opened == 0

        recorded = tmp_path / "heard.wav"
        command = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", str(recorded)]
        async with running(command, pane_environment(pymux)) as recorder:
            await once(
                lambda: devices.microphone_open,
                10.0,
                lambda: (
                    "the microphone never opened: recording=%s, links=%s, udp:\n%s"
                    % (pymux.audio.recording, list(pymux.audio.links), open("/proc/net/udp").read())
                ),
            )
            await anyio.sleep(2.0)
            link = next(iter(pymux.audio.links.values()))
            stats = "target %d ms, underruns %d, dropped %d" % (
                link.microphone.target_ms,
                link.microphone.underruns,
                link.microphone.dropped,
            )
            recorder.terminate()
            with anyio.fail_after(10):
                await recorder.wait()

        await once(lambda: not devices.microphone_open, 10.0, "the microphone stayed open")

    if os.environ.get("PYMUX_AUDIO_OUT"):
        shutil.copy(recorded, os.environ["PYMUX_AUDIO_OUT"])
    with wave.open(str(recorded)) as heard:
        pcm = heard.readframes(heard.getnframes())
    # The last second, after the buffers filled.
    last = pcm[-RATE * 2 :]
    assert abs(frequency(last, 1) - 880) < 5
    assert silent_frames(last) <= 3, "%d of %d frames silent, of %d recorded; server buffer %s" % (
        silent_frames(last),
        len(last) // (FRAME * 2),
        len(pcm) // (FRAME * 2),
        stats,
    )


async def test_the_buffers_follow_the_round_trip(tmp_path):
    "The client measures the round trip and tells the server what it buffers."
    async with serving(tmp_path) as pymux, carrying(pymux, FakeDevices()) as client:
        await once(lambda: len(client.round_trip) >= 3, 10.0, "no echo came back")
        link = next(iter(pymux.audio.links.values()))
        await once(
            lambda: link.microphone.target_ms == client.speaker_buffer.target_ms,
            10.0,
            lambda: "server %d ms, client %d ms" % (link.microphone.target_ms, client.speaker_buffer.target_ms),
        )


async def test_a_new_pane_finds_the_private_pipewire(tmp_path):
    async with serving(tmp_path) as pymux:
        out = tmp_path / "env"
        script = (
            "import os, pathlib; p = pathlib.Path(%r); "
            "p.with_suffix('.part').write_text(os.environ.get('PIPEWIRE_REMOTE', '') + '\\n' + os.environ.get('PULSE_SERVER', '')); "
            "os.replace(p.with_suffix('.part'), p)"
        ) % (str(out),)
        await pymux.create_window("%s -c %s" % (sys.executable, "'" + script.replace("'", "'\\''") + "'"))
        await once(out.exists, 10.0, "the pane never wrote its environment")
        remote, pulse = out.read_text().split("\n")
        assert remote == os.path.join(pymux.audio.runtime, "pipewire-0")
        assert pulse == "unix:" + os.path.join(pymux.audio.runtime, "pulse-native")
        assert os.path.exists(remote)


async def test_a_linux_client_plays_and_records_through_its_own_pipewire(tmp_path):
    """
    `pw-cat` on the client's side, into a PipeWire that stands for the
    person's desktop. That PipeWire is a second pymux server's, whose own
    fake client is the person's ears and voice:

        pane on A -> client (pw-cat) -> B's speaker -> B's fake client
        recorder on A <- client (pw-cat) <- B's microphone <- B's fake client
    """
    ears = FakeDevices(microphone_hz=880)
    async with (
        serving(tmp_path, name="a.sock") as remote,
        serving(tmp_path, name="b.sock") as desktop,
        carrying(desktop, ears, client_id="the-person"),
        carrying(remote, PipeWireDevices(programs()["pw-cat"], pane_environment(desktop))),
    ):
        song = tmp_path / "a440.wav"
        write_wav(song, tone(440, 4.0, 2), 2)
        async with running(["pw-play", str(song)], pane_environment(remote)):
            await once(
                lambda: len(ears.played) >= 2.5 * STEREO_SECOND, 20.0, lambda: "heard %d bytes" % len(ears.played)
            )
        if os.environ.get("PYMUX_AUDIO_OUT"):
            write_wav(os.path.join(os.environ["PYMUX_AUDIO_OUT"], "chain-played.wav"), bytes(ears.played), 2)
        middle = bytes(ears.played[STEREO_SECOND : 2 * STEREO_SECOND])
        assert abs(frequency(middle, 2) - 440) < 5
        assert silent_frames(middle, 2) <= 3

        recorded = tmp_path / "heard.wav"
        command = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", str(recorded)]
        async with running(command, pane_environment(remote)) as recorder:
            await once(lambda: ears.microphone_open, 15.0, "the person's microphone never opened")
            await anyio.sleep(2.5)
            recorder.terminate()
            with anyio.fail_after(10):
                await recorder.wait()
        await once(lambda: not ears.microphone_open, 10.0, "the person's microphone stayed open")

    with wave.open(str(recorded)) as heard:
        pcm = heard.readframes(heard.getnframes())
    last = pcm[-RATE * 2 :]
    assert abs(frequency(last, 1) - 880) < 5
    assert silent_frames(last) <= 3, "%d of %d frames silent" % (silent_frames(last), len(last) // (FRAME * 2))


async def test_an_attached_client_is_told_the_server_has_sound(tmp_path):
    "Only then does a client open its audio connection: never to a server with nothing to play."
    heard: list[dict] = []
    pymux = Pymux()
    pymux.audio_server = AudioServerMode.PRIVATE
    pymux.socket_name = str(tmp_path / "pymux.sock")
    async with over_connection(pymux=pymux, read_packet=heard.append) as session:
        await session.attach("one", Size(rows=24, columns=80))
        await once(
            lambda: any(packet.get(Field.CMD) == Packet.AUDIO for packet in heard),
            20.0,
            lambda: "no offer; sound %s" % ("on" if pymux.audio.running.is_set() else "off",),
        )
