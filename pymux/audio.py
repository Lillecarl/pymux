"""
Sound between the panes and the machine a person sits at.

The server holds a speaker and a microphone in PipeWire, and carries
their sound to the client used last, as Opus, on the connection that
client attached on. Lillecarl/pymux#657.

**The server is a sound client too, and pymux only the transport.** The
speaker is a `pw-cat --record` that says it is an `Audio/Sink`, and the
microphone a `pw-cat --playback` that says it is an `Audio/Source`.
WirePlumber links programs to them as to any device, and PipeWire's own
clock paces both. Lillecarl/pymux#662.

`audio-server` says whose PipeWire holds the speaker:

- "private" starts a PipeWire and a WirePlumber of this server's own,
  and points the panes at them. It works on any Linux host, and two
  servers never fight over a default sink.
- "shared" adds the speaker and microphone to the PipeWire the server
  finds, and names them to the panes' Pulse clients.
- "auto" is private when pymux was built with PipeWire beside it, and
  shared otherwise.

Both `pw-cat`s are pymux's own processes in either case, so nothing is
loaded into a daemon somebody else owns.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import shutil
import sys
import tempfile
from collections import deque
from importlib import resources
from subprocess import DEVNULL, PIPE
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
from libpymux.protocol import Field, Packet

from pymux.enums import AudioFrame, AudioServerMode, ClientAudio
from pymux.jitter import (
    DEFAULT_FRAME_MS,
    ECHO_EVERY,
    FIRST_ECHO_EVERY,
    FIRST_ECHOES,
    FRAME_SIZES,
    JitterBuffer,
    RoundTrip,
    floor_ms,
    samples_per_frame,
    stats_every,
)

if TYPE_CHECKING:
    from opuslib import Encoder

    from pymux.main import Pymux
    from pymux.server import ServerConnection

try:
    import opuslib
except ImportError:
    opuslib = None

logger = logging.getLogger(__name__)

RATE = 48000
#: How much sound the server's PipeWire moves at once, whatever frame a
#: client takes: a client's `audio-frame` below it shortens the buffers
#: and the client's device, not this. Following the smallest client
#: would restart both devices on every change, and a 2.5 ms quantum
#: wakes the graph 400 times a second for good.
QUANTUM_MS = 10
QUANTUM = samples_per_frame(QUANTUM_MS)
#: The most a frame of Opus holds, 60 ms: what a decoder is offered.
MAX_FRAME = samples_per_frame(60)
SPEAKER_CHANNELS = 2
MICROPHONE_CHANNELS = 1

#: The `sound` packets a slow link may hold before the oldest go.
#: Sound that comes too late is worth less than the next sound.
SEND_QUEUE = 25

#: What a person reads when a program on the server starts or stops
#: hearing their microphone.
MICROPHONE_ON = "Microphone on: a program on the server records. (set-client-option audio playback keeps it off)"
MICROPHONE_OFF = "Microphone off."
MICROPHONE_SILENT = (
    "The microphone gives only silence: its input volume is at zero, or the system refused it"
    " (macOS: Privacy & Security > Microphone, then reopen the terminal)."
)

FRAME_RAISED = "Sound: frames of %g ms now, the link could not keep up. (set-client-option audio-frame sets one)"

#: An `auto` frame goes up a size after this much trouble -- gaps the
#: client heard, sound let go as late -- within this many seconds. The
#: gap a change itself makes is not counted for a while after it.
TROUBLE_TO_RAISE = 2
TROUBLE_WINDOW = 10.0
SETTLE_SECONDS = 3.0

#: How long a server waits to start PipeWire again after it ended.
RETRY_SECONDS = 5.0

#: The pipe to the microphone's `pw-cat` holds one page, 43 ms of mono:
#: what waits in it waits before the person's voice, and a recorder that
#: starts hears it first.
MICROPHONE_PIPE = 4096

#: A program records while the microphone's `pw-cat` takes sound: more
#: than its pipe holds within this many seconds. A pipe that only filled
#: once does not count.
RECORDING_WINDOW = 0.25
RECORDING_BYTES = MICROPHONE_PIPE + QUANTUM * 2

#: A unix socket path holds 108 bytes, and macOS's 104.
SOCKET_PATH_MAX = 104

#: Where the build writes the paths of the PipeWire it was given.
PROGRAMS_FILE = "audio-programs.json"

#: `fcntl`'s name for a pipe's size, which Python names on Linux only.
F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", 1031)


def programs() -> dict[str, str]:
    """
    The PipeWire programs this pymux can start, by name. The build names
    the ones it carries; otherwise the PATH is asked.
    """
    found: dict[str, str] = {}
    with contextlib.suppress(FileNotFoundError, ValueError):
        found.update(json.loads(resources.files("pymux").joinpath(PROGRAMS_FILE).read_text()))
    for name in ("pipewire", "wireplumber", "pw-cat"):
        if name not in found:
            path = shutil.which(name)
            if path:
                found[name] = path
    return found


def carried() -> bool:
    "Whether the build put a PipeWire beside this pymux."
    return resources.files("pymux").joinpath(PROGRAMS_FILE).is_file()


def resolve(mode: AudioServerMode, have: dict[str, str], serves_one_terminal: bool = False) -> AudioServerMode:
    """
    What `mode` comes to here: never auto, and off when it cannot run.

    **Off for `pymux integrated`.** Its panes and its terminal are on one
    machine, so a pane plays on that machine's own sound already, with
    no server's PipeWire and nothing to carry.
    """
    if opuslib is None or sys.platform != "linux" or "pipewire" not in have or serves_one_terminal:
        return AudioServerMode.OFF
    if mode == AudioServerMode.AUTO:
        mode = AudioServerMode.PRIVATE if carried() and "wireplumber" in have else AudioServerMode.SHARED
    if mode == AudioServerMode.PRIVATE and "wireplumber" not in have:
        return AudioServerMode.OFF
    return mode


def runtime_dir_of(server_socket: str | None) -> str:
    """
    Where a private PipeWire keeps its sockets: beside the server's own
    socket, so a pane keeps a working `PIPEWIRE_REMOTE` across
    `upgrade-server`. A temporary directory when that path is too long.
    """
    if server_socket:
        directory, name = os.path.split(server_socket)
        path = os.path.join(directory, "audio-%s" % (name,))
        if len(os.path.join(path, "pipewire-0")) < SOCKET_PATH_MAX:
            return path
    return tempfile.mkdtemp(prefix="pymux-audio-")


def daemon_config(runtime: str) -> dict[str, Any]:
    """
    A PipeWire of its own, with the Pulse protocol in the same process.

    `module-access` is not optional: without it a client connects and
    never gets its permissions, and waits for ever.
    """
    return {
        "context.properties": {
            "core.daemon": True,
            "core.name": "pipewire-0",
            "default.clock.rate": RATE,
            "default.clock.quantum": QUANTUM,
            "default.clock.min-quantum": QUANTUM // 2,
            "mem.allow-mlock": False,
            "support.dbus": False,
        },
        "context.spa-libs": {
            "audio.convert.*": "audioconvert/libspa-audioconvert",
            "support.*": "support/libspa-support",
        },
        "context.modules": [
            {"name": "libpipewire-module-protocol-native"},
            {"name": "libpipewire-module-metadata"},
            {"name": "libpipewire-module-spa-node-factory"},
            {"name": "libpipewire-module-client-node"},
            {"name": "libpipewire-module-client-device"},
            {"name": "libpipewire-module-access"},
            {"name": "libpipewire-module-adapter"},
            {"name": "libpipewire-module-link-factory"},
            {"name": "libpipewire-module-session-manager"},
            {
                "name": "libpipewire-module-protocol-pulse",
                "args": {"server.address": ["unix:" + os.path.join(runtime, "pulse-native")]},
            },
        ],
        # Nothing else drives the graph on a machine with no sound card.
        "context.objects": [
            {
                "factory": "spa-node-factory",
                "args": {
                    "factory.name": "support.node.driver",
                    "node.name": "pymux-clock",
                    "priority.driver": 1,
                },
            }
        ],
    }


#: WirePlumber's policy and nothing else: no sound card, Bluetooth or
#: camera to look for, no D-Bus, and nothing remembered between runs.
WIREPLUMBER_PROFILE = """\
wireplumber.profiles = {
  pymux = {
    inherits = [ policy, mixin.systemwide-session, mixin.stateless ]
    support.dbus = disabled
  }
}
"""


def write_config(config: dict[str, Any], out) -> None:
    """
    A PipeWire config, as JSON, which SPA-JSON reads.

    **Indented.** PipeWire 1.6.8 calls the module array of the same JSON
    on one line "malformed", and the speaker then carried no sound.
    """
    json.dump(config, out, indent=2)


def pw_cat(
    program: str, mode: str, channels: int, node: str, description: str, media_class: str, priority: int
) -> list[str]:
    """
    A `pw-cat` that is a device: raw 16-bit sound at 48 kHz on its pipe,
    one 20 ms quantum of latency, and a node programs see as a sink or
    a source. `priority.session` decides whether WirePlumber makes it
    the default: in a PipeWire of pymux's own it should, and in a shared
    one it should not take the person's default away.
    """
    properties = {
        "media.class": media_class,
        "node.name": node,
        "node.description": description,
        "priority.session": priority,
    }
    return [
        program,
        mode,
        "--raw",
        "--rate",
        str(RATE),
        "--channels",
        str(channels),
        "--format",
        "s16",
        "--latency",
        "%dms" % (QUANTUM_MS,),
        "-P",
        json.dumps(properties),
        "-",
    ]


class AudioLink:
    """
    One attached client's sound, on the connection it attached on.

    **One connection, and an urgent lane on it.** Sound and echoes are
    written ahead of anything that waits for the connection, a window
    pane's frame parts included, so they wait only for the part already
    going out. The client asked for no second connection.

    **The server measures the round trip.** It sends an echo, the client
    answers the moment it reads it, and the server sizes both buffers
    from what came back: its own for the microphone, and the client's
    speaker, which it is told.
    """

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection
        self.client_id = connection.client_id
        self._queue: deque[dict] = deque(maxlen=SEND_QUEUE)
        self._ready = anyio.Event()
        #: The client's `audio-frame`: the frames both ways are this long.
        self.frame_ms: float = DEFAULT_FRAME_MS
        #: Whether the frame is the client's `auto`, which this link raises
        #: when it cannot keep up, and when it last did, and the trouble it
        #: counted since: the times of gaps the client heard, and of sound
        #: let go as late here.
        self.adaptive = False
        #: The client's `audio-speaker-bitrate`, in bits a second.
        self.speaker_bitrate = 128000
        self._raised = -math.inf
        self._trouble: deque[float] = deque()
        self.microphone = JitterBuffer(frame_ms=self.frame_ms)
        self.round_trip = RoundTrip()
        self._multiplier = 2.0
        self._told_ms: float | None = None
        #: Sound frames queued for the client, and those a slow link let go.
        self.sent = 0
        self.late = 0
        self._decoder = opuslib.Decoder(RATE, MICROPHONE_CHANNELS) if opuslib is not None else None
        self.microphone_open = False
        #: Whether the client's microphone gave only zeros.
        self.silent = False

    def send(self, packet: dict) -> None:
        "Queue a packet. A full queue lets the oldest go."
        if len(self._queue) == self._queue.maxlen:
            self.late += 1
            self._trouble.append(anyio.current_time())
        self._queue.append(packet)
        self._ready.set()
        if packet.get(Field.CMD) == Packet.SOUND:
            self.sent += 1

    async def run(self) -> None:
        "Write what is queued, and measure the round trip, until the connection goes."
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(self._echo)
            tasks.start_soon(self._adapt)
            every = stats_every()
            if every is not None:
                tasks.start_soon(self._stats, every)
            while True:
                await self._ready.wait()
                self._ready = anyio.Event()
                while self._queue:
                    await self.connection._write_urgent(self._queue.popleft())

    async def _stats(self, every: float) -> None:
        while True:
            await anyio.sleep(every)
            logger.info(
                "Sound stats, client %s: round trip %s ms; sent %d frames, %d dropped as late; microphone %s.",
                self.client_id or "?",
                self.round_trip.describe(),
                self.sent,
                self.late,
                self.microphone.describe(),
            )

    async def _adapt(self) -> None:
        "Raise an `auto` frame while the link cannot keep up."
        while True:
            await anyio.sleep(1.0)
            now = anyio.current_time()
            if self.adaptive and self.struggles(now):
                self.connection.pymux.audio.raise_frame(self, now)

    async def _echo(self) -> None:
        for _ in range(FIRST_ECHOES):
            self._ask()
            await anyio.sleep(FIRST_ECHO_EVERY)
        while True:
            self._ask()
            await anyio.sleep(ECHO_EVERY)

    def _ask(self) -> None:
        # Ahead of queued sound: it measures the link, and the queue is ours.
        self._queue.appendleft({Field.CMD: Packet.ECHO, Field.TIME: anyio.current_time()})
        self._ready.set()

    def retarget(self, multiplier: float | None = None) -> None:
        "Size both buffers from the round trips, and tell the client its own."
        if multiplier is not None:
            self._multiplier = multiplier
        # The server's PipeWire hands sound over a quantum at a time, so
        # frames shorter than it come in bursts: the floor holds one.
        floor = max(floor_ms(self.frame_ms), QUANTUM_MS + self.frame_ms)
        self.microphone.retarget(self.round_trip.buffer_ms(self._multiplier, floor))
        told = self.microphone.target_ms
        if told != self._told_ms:
            self._told_ms = told
            self.send({Field.CMD: Packet.LATENCY, Field.BUFFER: told})
            logger.info(
                "Client %s: round trip %s ms, buffers %d ms.",
                self.client_id or "?",
                self.round_trip.describe(),
                told,
            )

    def reframe(self, frame_ms: float) -> None:
        "The client's frames are this long from now on."
        if frame_ms != self.frame_ms:
            self.frame_ms = frame_ms
            self.microphone.reframe(frame_ms)

    def struggles(self, now: float) -> bool:
        """
        Whether the link cannot keep up: trouble enough in the last
        `TROUBLE_WINDOW` seconds, and none of it from the gap the last
        change of frame made.
        """
        while self._trouble and self._trouble[0] < now - TROUBLE_WINDOW:
            self._trouble.popleft()
        recent = [at for at in self._trouble if at > self._raised + SETTLE_SECONDS]
        return len(recent) >= TROUBLE_TO_RAISE

    def raise_frame(self, now: float) -> float | None:
        "Go up one frame size, when `auto` and not at the longest. The new size, or None."
        if not self.adaptive:
            return None
        longer = [size for size in FRAME_SIZES if size > self.frame_ms]
        if not longer:
            return None
        self._raised = now
        self._trouble.clear()
        self.reframe(longer[0])
        return longer[0]

    def heard(self, packet: dict) -> None:
        kind = packet.get(Field.CMD)
        if kind == Packet.BEHIND:
            self._trouble.append(anyio.current_time())
        elif kind == Packet.ECHO:
            sent = packet.get(Field.TIME)
            if isinstance(sent, (int, float)):
                self.round_trip.heard(anyio.current_time() - sent)
                self.retarget()
        elif kind == Packet.SOUND and opuslib is not None and self._decoder is not None:
            data = packet.get(Field.DATA)
            if isinstance(data, bytes):
                try:
                    self.microphone.put(self._decoder.decode(data, MAX_FRAME))
                except opuslib.OpusError:
                    logger.warning("A microphone frame from client %s did not decode.", self.client_id or "?")
        elif kind == Packet.MICROPHONE and packet.get(Field.ERROR) == "silent":
            self.silent = True
            self.connection.pymux.audio.microphone_silent(self)

    def open_microphone(self, wanted: bool) -> bool:
        "Open or close the client's microphone. Whether that changed it."
        if wanted == self.microphone_open:
            return False
        self.microphone_open = wanted
        if not wanted:
            self.microphone.clear()
        self.send({Field.CMD: Packet.MICROPHONE, Field.DATA: wanted})
        return True


class AudioServer:
    "The speaker and microphone of one pymux server, while `audio-server` allows."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        #: What the server runs now: never auto.
        self.mode = AudioServerMode.OFF
        #: The audio connections, by client id.
        self.links: dict[str, AudioLink] = {}
        self.speaker_node = "pymux-speaker"
        self.microphone_node = "pymux-microphone"
        #: Where a private PipeWire listens, while one runs.
        self.runtime: str | None = None
        #: Whether a program records from the microphone. `_watch` keeps it.
        self.recording = False
        #: Milliseconds of a client's microphone given to PipeWire, ever.
        self.microphone_ms = 0.0
        self._changed = anyio.Event()
        self._scope: anyio.CancelScope | None = None
        #: Set once PipeWire listens, for whoever waits on it. A test does.
        self.running = anyio.Event()
        #: While an upgrade hands over, nothing starts.
        self.paused = False
        self._processes: list[anyio.abc.Process] = []

    # ------------------------------------------------------------------
    # What panes and clients see.

    def pane_environment(self, environment: dict[str, str]) -> None:
        "Point a new pane's sound at the speaker and the microphone."
        if self.mode == AudioServerMode.PRIVATE and self.runtime is not None:
            environment["PIPEWIRE_REMOTE"] = os.path.join(self.runtime, "pipewire-0")
            environment["PULSE_SERVER"] = "unix:" + os.path.join(self.runtime, "pulse-native")
        elif self.mode == AudioServerMode.SHARED:
            environment["PULSE_SINK"] = self.speaker_node
            environment["PULSE_SOURCE"] = self.microphone_node

    def link_opened(self, link: AudioLink) -> None:
        self.links[link.client_id] = link
        logger.info("Client %s plays sound.", link.client_id or "?")
        self._tell(link)
        self._steer_microphone()

    def link_closed(self, link: AudioLink) -> None:
        if self.links.get(link.client_id) is link:
            del self.links[link.client_id]
        self._steer_microphone()

    def client_options_changed(self) -> None:
        "A client's sound option moved."
        for link in self.links.values():
            self._tell(link)
        self._steer_microphone()

    def microphone_silent(self, link: AudioLink) -> None:
        "The client's microphone hands over only zeros: its system refused it."
        logger.warning("Client %s hears only silence from its microphone.", link.client_id or "?")
        client_state = self._client_of(link)
        if client_state is not None:
            client_state.message = MICROPHONE_SILENT
            self.pymux.invalidate()

    def _client_of(self, link: AudioLink):
        for client_state in self.pymux.clients:
            if client_state.connection.client_id == link.client_id:
                return client_state
        return None

    def _audio_of(self, link: AudioLink) -> ClientAudio:
        client_state = self._client_of(link)
        return client_state.audio if client_state is not None else ClientAudio.ON

    def _tell(self, link: AudioLink) -> None:
        client_state = self._client_of(link)
        if client_state is None:
            return
        frame = AudioFrame(client_state.audio_frame)
        if frame is AudioFrame.AUTO:
            # A link that already raised its frame keeps it.
            frame_ms = link.frame_ms if link.adaptive else frame.ms
            link.adaptive = True
        else:
            frame_ms = frame.ms
            link.adaptive = False
        link.reframe(frame_ms)
        link.speaker_bitrate = client_state.audio_speaker_bitrate * 1000
        link.send(
            {
                Field.CMD: Packet.AUDIO,
                Field.DATA: str(client_state.audio),
                Field.FRAME: frame_ms,
                Field.BITRATE: client_state.audio_microphone_bitrate * 1000,
            }
        )
        link.retarget(client_state.audio_rtt_multiplier)

    def raise_frame(self, link: AudioLink, now: float) -> None:
        "An `auto` link cannot keep up: frames one size longer, and the person told."
        frame_ms = link.raise_frame(now)
        if frame_ms is None:
            return
        logger.info("Client %s cannot keep up: frames of %g ms now.", link.client_id or "?", frame_ms)
        self._tell(link)
        client_state = self._client_of(link)
        if client_state is not None:
            client_state.message = FRAME_RAISED % (frame_ms,)
            self.pymux.invalidate()

    def current(self) -> AudioLink | None:
        """
        The link of the client used last, or the newest link when that
        client has none. A client whose `audio` is off is never it.
        """
        playing = {client_id: link for client_id, link in self.links.items() if self._audio_of(link) != ClientAudio.OFF}
        latest = self.pymux.latest_client()
        client_id = getattr(getattr(latest, "connection", None), "client_id", None)
        if client_id and client_id in playing:
            return playing[client_id]
        return next(reversed(playing.values()), None)

    def _steer_microphone(self) -> None:
        "Only the current client's microphone is open, and only while a program records."
        current = self.current()
        for link in self.links.values():
            lends = self._audio_of(link) == ClientAudio.ON
            wanted = self.recording and lends and link is current
            client_state = self._client_of(link)
            if link.open_microphone(wanted) and client_state is not None:
                # A microphone is never opened unannounced.
                client_state.message = MICROPHONE_ON if wanted else MICROPHONE_OFF
                self.pymux.invalidate()

    # ------------------------------------------------------------------
    # Running.

    def option_changed(self) -> None:
        "`audio-server` moved: stop, and start again as it says."
        self._changed.set()
        if self._scope is not None:
            self._scope.cancel()

    def pids(self) -> list[int]:
        "The programs sound runs now. `list-processes` names them."
        return [process.pid for process in self._processes]

    def pause(self) -> None:
        """
        Stop PipeWire now, for `upgrade-server`. The old server leaves
        with `os._exit`, which would orphan both programs, and the new
        one starts its own in the same directory.
        """
        self.paused = True
        for process in list(self._processes):
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        self.option_changed()

    def resume(self) -> None:
        "An upgrade that failed: sound again."
        self.paused = False
        self._changed.set()

    async def keep(self) -> None:
        "Run PipeWire whenever the option allows. Never returns."
        while True:
            have = programs()
            self.mode = resolve(self.pymux.audio_server, have, self.pymux._serves_one_terminal)
            failed = False
            if self.mode != AudioServerMode.OFF and not self.paused:
                with anyio.CancelScope() as self._scope:
                    try:
                        await self._serve(have)
                    except Exception:
                        logger.exception("Sound stopped.")
                        failed = True
                self._scope = None
                self.mode = AudioServerMode.OFF
            # A PipeWire that crashed comes back on its own.
            with anyio.move_on_after(RETRY_SECONDS if failed and not self.paused else math.inf):
                await self._changed.wait()
            self._changed = anyio.Event()

    async def _serve(self, have: dict[str, str]) -> None:
        if opuslib is None or "pw-cat" not in have:
            return
        shared = self.mode == AudioServerMode.SHARED
        suffix = "-%d" % (os.getpid(),) if shared else ""
        self.speaker_node, self.microphone_node = "pymux-speaker" + suffix, "pymux-microphone" + suffix
        runtime = None if shared else runtime_dir_of(self.pymux.socket_name)
        work = tempfile.mkdtemp(prefix="pymux-audio-conf-")
        encoder = opuslib.Encoder(RATE, SPEAKER_CHANNELS, opuslib.APPLICATION_AUDIO)
        try:
            async with anyio.create_task_group() as tasks:
                environment = dict(os.environ)
                if runtime is not None:
                    os.makedirs(runtime, mode=0o700, exist_ok=True)
                    await self._start_private(tasks, have, runtime, work)
                    self.runtime = runtime
                    environment["PIPEWIRE_REMOTE"] = os.path.join(runtime, "pipewire-0")
                priority = 1 if shared else 2000
                speaker = pw_cat(
                    have["pw-cat"],
                    "--record",
                    SPEAKER_CHANNELS,
                    self.speaker_node,
                    "pymux: the client's speaker",
                    "Audio/Sink",
                    priority,
                )
                microphone = pw_cat(
                    have["pw-cat"],
                    "--playback",
                    MICROPHONE_CHANNELS,
                    self.microphone_node,
                    "pymux: the client's microphone",
                    "Audio/Source",
                    priority,
                )
                tasks.start_soon(self._speak, speaker, environment, encoder)
                tasks.start_soon(self._listen, microphone, environment)
                logger.info("Sound on (%s PipeWire): %s and %s.", self.mode, self.speaker_node, self.microphone_node)
                self.running.set()
        finally:
            self.running = anyio.Event()
            self.runtime = None
            self.recording = False
            self._steer_microphone()
            shutil.rmtree(work, ignore_errors=True)

    async def _start_private(self, tasks: anyio.abc.TaskGroup, have: dict[str, str], runtime: str, work: str) -> None:
        conf = os.path.join(work, "pipewire.conf")
        with open(conf, "w") as out:
            write_config(daemon_config(runtime), out)
        config_home = os.path.join(work, "config")
        os.makedirs(os.path.join(config_home, "wireplumber", "wireplumber.conf.d"))
        with open(os.path.join(config_home, "wireplumber", "wireplumber.conf.d", "pymux.conf"), "w") as out:
            out.write(WIREPLUMBER_PROFILE)
        state = os.path.join(work, "state")
        os.makedirs(state)

        environment = {
            **os.environ,
            "PIPEWIRE_RUNTIME_DIR": runtime,
            "XDG_RUNTIME_DIR": runtime,
            "XDG_STATE_HOME": state,
            "XDG_CONFIG_HOME": config_home,
            "PIPEWIRE_REMOTE": os.path.join(runtime, "pipewire-0"),
            "DBUS_SESSION_BUS_ADDRESS": "disabled:",
        }
        for leftover in (
            "pipewire-0",
            "pipewire-0.lock",
            "pipewire-0-manager",
            "pipewire-0-manager.lock",
            "pulse-native",
        ):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(runtime, leftover))
        tasks.start_soon(self._run, [have["pipewire"], "-c", conf], environment, "pipewire")
        with anyio.fail_after(10):
            while not os.path.exists(os.path.join(runtime, "pipewire-0")):
                await anyio.sleep(0.05)
        tasks.start_soon(self._run, [have["wireplumber"], "-p", "pymux"], environment, "wireplumber")

    async def _run(self, argv: list[str], environment: dict[str, str], name: str) -> None:
        "Run one program for as long as sound is on. One that ends ends sound."
        async with self._program(argv, environment) as process:
            code = await process.wait()
        raise RuntimeError("%s ended with %s" % (name, code))

    @contextlib.asynccontextmanager
    async def _program(self, argv: list[str], environment: dict[str, str], stdin=DEVNULL, stdout=DEVNULL):
        "A program of sound's, ended with the block. `pause` can end it sooner."
        process = await anyio.open_process(argv, env=environment, stdin=stdin, stdout=stdout, stderr=None)
        self._processes.append(process)
        try:
            yield process
        finally:
            self._processes.remove(process)
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(2, shield=True):
                    await process.wait()
                if process.returncode is None:
                    process.kill()

    async def _speak(self, argv: list[str], environment: dict[str, str], encoder: Encoder) -> None:
        """
        What panes play, from the speaker's `pw-cat` to the current
        client as Opus, in frames as long as that client takes and at
        its bitrate. Opus takes a new bitrate between two frames.
        """
        held = bytearray()
        bitrate = None
        async with self._program(argv, environment, stdout=PIPE) as process:
            assert process.stdout is not None
            async for chunk in process.stdout:
                held += chunk
                while True:
                    link = self.current()
                    samples = samples_per_frame(link.frame_ms if link is not None else DEFAULT_FRAME_MS)
                    whole = samples * SPEAKER_CHANNELS * 2
                    if len(held) < whole:
                        break
                    pcm = bytes(held[:whole])
                    del held[:whole]
                    if link is not None:
                        if bitrate != link.speaker_bitrate:
                            bitrate = encoder.bitrate = link.speaker_bitrate
                        link.send(
                            {
                                Field.CMD: Packet.SOUND,
                                Field.DATA: encoder.encode(pcm, samples),
                                Field.CHANNELS: SPEAKER_CHANNELS,
                            }
                        )
        raise RuntimeError("the speaker's pw-cat ended")

    async def _listen(self, argv: list[str], environment: dict[str, str]) -> None:
        """
        The current client's microphone, into the microphone's `pw-cat`
        as fast as it takes it, and silence when there is none.

        **Its pipe says whether a program records.** PipeWire runs the
        microphone's node only while something reads from it, so `pw-cat`
        takes sound then and none otherwise.
        """
        read_end, write_end = os.pipe()
        with contextlib.suppress(OSError):
            fcntl.fcntl(write_end, F_SETPIPE_SZ, MICROPHONE_PIPE)
        os.set_blocking(write_end, False)
        silence = bytes(QUANTUM * MICROPHONE_CHANNELS * 2)
        taken: deque[tuple[float, int]] = deque()
        try:
            async with self._program(argv, environment, stdin=read_end) as process, anyio.create_task_group() as tasks:
                os.close(read_end)
                read_end = -1
                tasks.start_soon(self._watch, taken)
                pending = b""
                while process.returncode is None:
                    if not pending:
                        self._steer_microphone()
                        link = self.current()
                        frame = link.microphone.take() if link is not None and link.microphone_open else None
                        if frame is not None and link is not None:
                            self.microphone_ms += link.frame_ms
                        pending = frame if frame is not None else silence
                    await anyio.wait_writable(write_end)
                    try:
                        written = os.write(write_end, pending)
                    except BlockingIOError:
                        continue
                    pending = pending[written:]
                    taken.append((anyio.current_time(), written))
        finally:
            for fd in (read_end, write_end):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
        raise RuntimeError("the microphone's pw-cat ended")

    async def _watch(self, taken: deque[tuple[float, int]]) -> None:
        "Whether a program records: whether the microphone's pipe drains."
        while True:
            await anyio.sleep(QUANTUM_MS / 1000)
            now = anyio.current_time()
            while taken and taken[0][0] < now - RECORDING_WINDOW:
                taken.popleft()
            recording = sum(written for _at, written in taken) > RECORDING_BYTES
            if recording != self.recording:
                self.recording = recording
                logger.info("A program %s the microphone.", "records from" if recording else "stopped recording from")
                self._steer_microphone()
