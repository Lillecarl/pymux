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

import contextlib
import math
import os
import shutil
import socket
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
from libpymux.wire import PacketReader, encode_json, encode_msgpack
from prompt_toolkit.output import ColorDepth

from pymux.audio import programs
from pymux.client.audio import AudioClient, PipeWireDevices
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


#: The frequencies `loudest` weighs, in Hz.
CANDIDATES = range(200, 1600, 10)


def loudest(pcm: bytes, channels: int) -> int:
    """
    The loudest frequency in the middle quarter second of the first
    channel, by Goertzel.

    Not zero crossings. A glitch -- frames a lagging consumer dropped at
    the buffer's cap -- adds crossings that are not the tone, and under
    the gate's load that was most of a second. A glitch spreads its
    energy over every frequency; a wrong rate moves the peak.
    """
    window = _middle(pcm, channels)
    return max(CANDIDATES, key=lambda hz: _power(window, hz))


def sidebands(pcm: bytes, channels: int, hz: int) -> float:
    """
    The strongest of the tone's neighbours 50 Hz away, against the tone,
    in amplitude. A cut in every 20 ms packet puts them at 0.42; a run
    under load measured 0.05, and a clean one 0.004.
    """
    window = _middle(pcm, channels)
    tone = _power(window, hz)
    return math.sqrt(max(_power(window, hz - 50), _power(window, hz + 50)) / tone) if tone else 1.0


def _middle(pcm: bytes, channels: int) -> tuple[int, ...]:
    every = struct.unpack("<%dh" % (len(pcm) // 2), pcm)[::channels]
    middle = len(every) // 2
    return every[middle - RATE // 8 : middle + RATE // 8]


def _power(window: tuple[int, ...], hz: float) -> float:
    "Goertzel: the power of one frequency in a window."
    coefficient = 2 * math.cos(2 * math.pi * hz / RATE)
    before = earlier = 0.0
    for sample in window:
        before, earlier = sample + coefficient * before - earlier, before
    return before * before + earlier * earlier - coefficient * before * earlier


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


class Attached:
    """
    A client attached over the server's socket that plays sound, and
    nothing else a terminal does: what `client/terminal.py` sends and
    answers about sound, with its writes locked as a real client's are.
    """

    def __init__(self, path: str, devices, client_id: str) -> None:
        self.client_id = client_id
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self._lock = threading.Lock()
        self._encode = encode_json
        self.audio = AudioClient(devices, self.send)

    def send(self, packet: dict) -> None:
        with self._lock:
            with contextlib.suppress(OSError):
                self.sock.sendall(self._encode(packet))

    def attach(self) -> None:
        self.send({"cmd": "size", "data": [24, 80]})
        self.send(
            {
                "cmd": "start-gui",
                "detach-others": False,
                "color-depth": ColorDepth.DEPTH_8_BIT,
                "term": "xterm-256color",
                "client-id": self.client_id,
                "wire": ["msgpack"],
                "audio": True,
                "data": "",
            }
        )

    async def read(self) -> None:
        reader = PacketReader()
        while True:
            await anyio.wait_readable(self.sock)
            data = self.sock.recv(1 << 20)
            if not data:
                return
            for packet in reader.feed(data):
                kind = packet.get(Field.CMD)
                if kind == Packet.WIRE:
                    with self._lock:
                        self.sock.sendall(self._encode({"cmd": "wire", "wire": "msgpack"}))
                        self._encode = encode_msgpack
                elif kind == Packet.ECHO:
                    self.send({"cmd": "echo", "time": packet.get(Field.TIME)})
                else:
                    self.audio.heard(packet)

    def close(self) -> None:
        self.audio.close()
        self.sock.close()


@asynccontextmanager
async def carrying(pymux, devices, client_id="the-client"):
    "A client attached to `pymux` that plays sound, until the block ends."
    client = Attached(pymux.socket_name, devices, client_id)
    client.attach()
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(client.read)
            await once(lambda: client_id in pymux.audio.links, 10.0, "the client never played sound")
            try:
                yield client
            finally:
                tasks.cancel_scope.cancel()
    finally:
        # After the reader: a socket closed under its wait reads EBADF.
        client.close()


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


async def heard_for(pymux, seconds: float) -> None:
    """
    Wait until PipeWire has had this much of the microphone. Not a sleep:
    under load the server measures a long round trip and buffers up to
    a second before the first frame, and a recording that stopped on the
    clock held only silence.
    """
    start = pymux.audio.microphone_frames
    await once(
        lambda: pymux.audio.microphone_frames - start >= seconds * 1000 / 20,
        30.0,
        lambda: "PipeWire had %d frames of the microphone" % (pymux.audio.microphone_frames - start,),
    )


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
    assert loudest(middle, 2) == 440
    assert sidebands(middle, 2, 440) < 0.25
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
            await heard_for(pymux, 1.5)
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
    assert loudest(last, 1) == 880
    assert sidebands(last, 1, 880) < 0.25
    assert silent_frames(last) <= 3, "%d of %d frames silent, of %d recorded; server buffer %s" % (
        silent_frames(last),
        len(last) // (FRAME * 2),
        len(pcm) // (FRAME * 2),
        stats,
    )


async def test_the_server_sizes_both_buffers_from_the_round_trip(tmp_path):
    """
    The server measures the round trip with echoes the client answers,
    and tells the client what its speaker holds. A multiplier this large
    pins both at the 1000 ms ceiling, so only a `latency` packet makes
    the client's match the server's.
    """
    async with serving(tmp_path) as pymux, carrying(pymux, FakeDevices()) as client:
        link = next(iter(pymux.audio.links.values()))
        await once(lambda: len(link.round_trip) >= 3, 10.0, "no echo came back")
        for client_state in pymux.clients:
            client_state.audio_rtt_multiplier = 1e6
        pymux.audio.client_options_changed()
        speaker = client.audio.speaker_buffer
        await once(
            lambda: link.microphone.target_ms == speaker.target_ms == 1000,
            10.0,
            lambda: "server %d ms, client %d ms" % (link.microphone.target_ms, speaker.target_ms),
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

    Pitch and purity, not gaps: two hops of Python threads under the
    gate's load leave gaps that measure the load. The one-hop tests
    above bound them.
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
        assert loudest(middle, 2) == 440
        assert sidebands(middle, 2, 440) < 0.25

        recorded = tmp_path / "heard.wav"
        command = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", str(recorded)]
        async with running(command, pane_environment(remote)) as recorder:
            await once(lambda: ears.microphone_open, 15.0, "the person's microphone never opened")
            await heard_for(remote, 1.5)
            recorder.terminate()
            with anyio.fail_after(10):
                await recorder.wait()
        await once(lambda: not ears.microphone_open, 10.0, "the person's microphone stayed open")

    with wave.open(str(recorded)) as heard:
        pcm = heard.readframes(heard.getnframes())
    last = pcm[-RATE * 2 :]
    assert loudest(last, 1) == 880
    assert sidebands(last, 1, 880) < 0.25


async def test_a_client_that_plays_no_sound_is_sent_none(tmp_path):
    "One that did not say `audio` in `start-gui`, an older one included: no link, and no sound packet."
    heard: list[dict] = []
    pymux = Pymux()
    pymux.audio_server = AudioServerMode.PRIVATE
    pymux.socket_name = str(tmp_path / "pymux.sock")
    async with over_connection(pymux=pymux, read_packet=heard.append) as session:
        # Sound first: PipeWire starting beside an attach under load took
        # longer than the attach may.
        with anyio.fail_after(20):
            await pymux.audio.running.wait()
        await session.attach("one", Size(rows=24, columns=80), frames=True)
        song = tmp_path / "a440.wav"
        write_wav(song, tone(440, 1.0, 2), 2)
        async with running(["pw-play", str(song)], pane_environment(pymux)) as player:
            with anyio.fail_after(10):
                await player.wait()
        assert not pymux.audio.links
        sound = {Packet.SOUND, Packet.AUDIO, Packet.ECHO, Packet.LATENCY, Packet.MICROPHONE}
        assert not [packet for packet in heard if packet.get(Field.CMD) in sound]


@pytest.mark.skipif(not shutil.which("paplay"), reason="no Pulse client on the PATH")
async def test_a_pulse_program_plays_and_records(tmp_path):
    "Most programs reach a speaker through Pulse, which a pane finds in PULSE_SERVER."
    devices = FakeDevices(microphone_hz=880)
    async with serving(tmp_path) as pymux, carrying(pymux, devices):
        song = tmp_path / "a440.wav"
        write_wav(song, tone(440, 3.0, 2), 2)
        async with running(["paplay", str(song)], pane_environment(pymux)):
            await once(
                lambda: len(devices.played) >= 2.5 * STEREO_SECOND,
                15.0,
                lambda: "played %d bytes" % len(devices.played),
            )
        middle = bytes(devices.played[STEREO_SECOND : 2 * STEREO_SECOND])
        assert loudest(middle, 2) == 440

        recorded = tmp_path / "heard.wav"
        # A short fragment: by default Pulse hands a recorder two
        # seconds at a time, and one stopped sooner wrote nothing.
        command = [
            "parecord",
            "--rate=48000",
            "--channels=1",
            "--format=s16le",
            "--file-format=wav",
            "--latency-msec=40",
            str(recorded),
        ]
        async with running(command, pane_environment(pymux)) as recorder:
            await once(lambda: devices.microphone_open, 10.0, "the microphone never opened for Pulse")
            await heard_for(pymux, 1.5)
            recorder.terminate()
            with anyio.fail_after(10):
                await recorder.wait()

    with wave.open(str(recorded)) as heard:
        pcm = heard.readframes(heard.getnframes())
    last = pcm[-RATE * 2 :]
    assert loudest(last, 1) == 880


async def test_sound_reaches_a_client_over_ssh_and_after_the_link_comes_back(monkeypatch):
    """
    The route a person on another machine takes: an SSH client plays
    sound on the one channel it attached on. After the link drops and
    comes back, the new connection's buffers are sized again, on both
    ends.
    """
    from test_the_link_comes_back import attached

    devices = FakeDevices()
    pymux = Pymux()
    pymux.test_mode = True
    pymux.audio_server = AudioServerMode.PRIVATE

    def with_sound(client) -> None:
        client.audio = AudioClient(devices, client._send_from_a_thread)
        # As a person's configuration file would, on every attach: the
        # buffers pin at the 1000 ms ceiling, the same on both links.
        client._client_options = lambda: [("audio-rtt-multiplier", "1000000")]

    async with attached(monkeypatch, pymux=pymux, client_setup=with_sound) as it:
        await it.until(lambda: pymux.audio.links, "the client to play sound", seconds=30.0)
        tone_file = os.path.join(os.path.dirname(pymux.socket_name), "a440.wav")
        write_wav(tone_file, tone(440, 3.0, 2), 2)
        async with running(["pw-play", tone_file], pane_environment(pymux)):
            await it.until(lambda: len(devices.played) >= 2.5 * STEREO_SECOND, "the sound over SSH")
        assert loudest(bytes(devices.played[STEREO_SECOND : 2 * STEREO_SECOND]), 2) == 440

        await it.until(lambda: it.client.audio.speaker_buffer.target_ms == 1000, "the pinned buffer")
        first = next(iter(pymux.audio.links.values()))
        it.drop_the_link()
        await it.until(lambda: first not in pymux.audio.links.values(), "the old link to go")
        it.terminal.type("\r")
        await it.until(lambda: pymux.audio.links and pymux.clients, "the client to play sound again", seconds=30.0)
        link = next(iter(pymux.audio.links.values()))
        audio = it.client.audio
        await it.until(
            lambda: audio.speaker_buffer.target_ms == 1000 and link.microphone.target_ms == 1000,
            "the new link to hear the buffer",
        )


async def test_a_microphone_that_gives_only_zeros_is_reported(tmp_path):
    "macOS refuses a terminal the microphone with zeros and no error."
    devices = FakeDevices(microphone_hz=0)
    async with serving(tmp_path) as pymux, carrying(pymux, devices):
        command = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", str(tmp_path / "x.wav")]
        async with running(command, pane_environment(pymux)):
            await once(lambda: devices.microphone_open, 10.0, "the microphone never opened")
            link = next(iter(pymux.audio.links.values()))
            await once(lambda: link.silent, 10.0, "the silence was never reported")
