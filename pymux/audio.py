"""
Sound between the panes and the machine a person sits at.

The server holds a speaker and a microphone in PipeWire, and carries
their sound to the client used last, as Opus, on a connection of its
own. Lillecarl/pymux#657.

PipeWire speaks raw L16 RTP on loopback, and pymux encodes Opus itself.
PipeWire's own Opus is multistream with every channel uncoupled, which
a plain Opus decoder reads as noise, and its packet time ignores
`rtp.ptime`. So the wire carries `sound` packets of 20 ms of Opus, and
only this module knows RTP.

`audio-server` says whose PipeWire holds the speaker:

- "private" starts a PipeWire and a WirePlumber of this server's own,
  and points the panes at them. It works on any Linux host, and two
  servers never fight over a default sink.
- "shared" adds the speaker and microphone to the PipeWire the server
  finds, and names them to the panes' Pulse clients.
- "auto" is private when pymux was built with PipeWire beside it, and
  shared otherwise.

The RTP modules run in a process of pymux's own in both cases, so
nothing is loaded into a daemon somebody else owns.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import struct
import sys
import tempfile
from array import array
from collections import deque
from importlib import resources
from subprocess import DEVNULL
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import anyio.to_thread
from libpymux.protocol import Field, Packet

from pymux.enums import AudioServerMode, ClientAudio
from pymux.jitter import FRAME_MS, JitterBuffer

if TYPE_CHECKING:
    from pymux.main import Pymux
    from pymux.server import ServerConnection

try:
    import opuslib
except ImportError:
    opuslib = None

logger = logging.getLogger(__name__)

RATE = 48000
FRAME = RATE * FRAME_MS // 1000
SPEAKER_CHANNELS = 2
MICROPHONE_CHANNELS = 1
#: Enough for music. Opus spends less on silence by itself.
SPEAKER_BITRATE = 128000
MICROPHONE_BITRATE = 64000

#: The microphone's buffer in PipeWire, which stays fixed: the buffer
#: that follows the round trip is pymux's own (`JitterBuffer`), because
#: `module-rtp-source` reads `sess.latency.msec` only when it loads.
SOURCE_LATENCY_MS = 2 * FRAME_MS

#: The `sound` packets a slow link may hold before the oldest go.
#: Sound that comes too late is worth less than the next sound.
SEND_QUEUE = 25

#: How often the server looks whether a program records, in seconds.
WATCH_EVERY = 0.25

#: A unix socket path holds 108 bytes, and macOS's 104.
SOCKET_PATH_MAX = 104

#: Where the build writes the paths of the PipeWire it was given.
PROGRAMS_FILE = "audio-programs.json"

L16_PAYLOAD = 127
_RTP = struct.Struct("!BBHII")


def programs() -> dict[str, str]:
    """
    The PipeWire programs this pymux can start, by name. The build names
    the ones it carries; otherwise the PATH is asked.
    """
    found: dict[str, str] = {}
    with contextlib.suppress(FileNotFoundError, ValueError):
        found.update(json.loads(resources.files("pymux").joinpath(PROGRAMS_FILE).read_text()))
    for name in ("pipewire", "wireplumber"):
        if name not in found:
            path = shutil.which(name)
            if path:
                found[name] = path
    return found


def carried() -> bool:
    "Whether the build put a PipeWire beside this pymux."
    return resources.files("pymux").joinpath(PROGRAMS_FILE).is_file()


def resolve(mode: AudioServerMode, have: dict[str, str]) -> AudioServerMode:
    "What `mode` comes to here: never auto, and off when it cannot run."
    if opuslib is None or sys.platform != "linux" or "pipewire" not in have:
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


def rtp_sink(port: int, node: str) -> dict[str, Any]:
    "The speaker: what plays into it goes to `port` as L16 RTP, 20 ms a packet."
    return {
        "name": "libpipewire-module-rtp-sink",
        "args": {
            "destination.ip": "127.0.0.1",
            "destination.port": port,
            "source.ip": "127.0.0.1",
            "net.mtu": 8192,
            "sess.min-ptime": FRAME_MS,
            "sess.max-ptime": FRAME_MS,
            "sess.name": node,
            "audio.format": "S16BE",
            "audio.rate": RATE,
            "audio.channels": SPEAKER_CHANNELS,
            "audio.position": ["FL", "FR"],
            "stream.props": {
                "node.name": node,
                "node.description": "pymux: the client's speaker",
                "media.class": "Audio/Sink",
            },
        },
    }


def rtp_source(port: int, node: str) -> dict[str, Any]:
    """
    The microphone: L16 RTP sent from `127.0.0.1:port` to the same port.

    **From that port and no other.** With a unicast `source.ip` the module
    `connect()`s its socket to `source.ip:source.port`, so it takes
    packets only from there; the sender binds it too, with `SO_REUSEADDR`
    as the module does.
    """
    return {
        "name": "libpipewire-module-rtp-source",
        "args": {
            "source.ip": "127.0.0.1",
            "source.port": port,
            # Its receive buffer is one MTU. At the default 1280 bytes it
            # cut every 20 ms packet (1932 bytes) short, and a tone came
            # out with sidebands every 50 Hz.
            "net.mtu": 8192,
            "sess.latency.msec": SOURCE_LATENCY_MS,
            "sess.ignore-ssrc": True,
            "audio.format": "S16BE",
            "audio.rate": RATE,
            "audio.channels": MICROPHONE_CHANNELS,
            "audio.position": ["MONO"],
            "stream.props": {
                "node.name": node,
                "node.description": "pymux: the client's microphone",
                "media.class": "Audio/Source",
            },
        },
    }


def daemon_config(runtime: str, rtp: list[dict[str, Any]]) -> dict[str, Any]:
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
            "default.clock.quantum": FRAME,
            "default.clock.min-quantum": FRAME // 2,
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
            *rtp,
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


def host_config(rtp: list[dict[str, Any]]) -> dict[str, Any]:
    "A process that only holds the RTP modules, in a PipeWire somebody else runs."
    return {
        "context.properties": {"core.daemon": False, "core.name": "pymux-audio", "support.dbus": False},
        "context.spa-libs": {"audio.convert.*": "audioconvert/libspa-audioconvert", "support.*": "support/libspa-support"},
        "context.modules": [
            {"name": "libpipewire-module-protocol-native"},
            {"name": "libpipewire-module-client-node"},
            {"name": "libpipewire-module-adapter"},
            *rtp,
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


def _listens_for(port: int) -> bool:
    """
    Whether a UDP socket on this machine is connected to 127.0.0.1:port
    from that same port: `module-rtp-source`'s, while it listens.

    Its socket is bound to 0.0.0.0, and the table shows 127.0.0.1 all the
    same: `connect()` gives a socket bound to any address the source
    address of its route. The sender's own socket has no peer.
    """
    loopback = "0100007F:%04X" % (port,)
    try:
        with open("/proc/net/udp") as table:
            next(table, None)
            for line in table:
                fields = line.split()
                if len(fields) > 2 and fields[1] == loopback and fields[2] == loopback:
                    return True
    except OSError:
        pass
    return False


def l16_to_native(payload: bytes) -> bytes:
    samples = array("h")
    samples.frombytes(payload[: len(payload) // 2 * 2])
    if sys.byteorder == "little":
        samples.byteswap()
    return samples.tobytes()


native_to_l16 = l16_to_native


class AudioLink:
    "One client's audio connection, as the server holds it."

    def __init__(self, connection: ServerConnection, client_id: str) -> None:
        self.connection = connection
        self.client_id = client_id
        self._queue: deque[dict] = deque(maxlen=SEND_QUEUE)
        self._ready = anyio.Event()
        self.microphone = JitterBuffer()
        self._decoder = opuslib.Decoder(RATE, MICROPHONE_CHANNELS) if opuslib is not None else None
        self.microphone_open = False

    def send(self, packet: dict) -> None:
        "Queue a packet. A full queue lets the oldest go."
        self._queue.append(packet)
        self._ready.set()

    async def run(self) -> None:
        "Write what is queued, until the connection goes."
        while True:
            await self._ready.wait()
            self._ready = anyio.Event()
            while self._queue:
                await self.connection._write_packet(self._queue.popleft())

    def heard(self, packet: dict) -> None:
        kind = packet.get(Field.CMD)
        if kind == Packet.ECHO:
            # Ahead of queued sound: the round trip measures the link,
            # and this queue is ours.
            self._queue.appendleft({Field.CMD: Packet.ECHO, Field.TIME: packet.get(Field.TIME)})
            self._ready.set()
        elif kind == Packet.LATENCY:
            buffer = packet.get(Field.BUFFER)
            if isinstance(buffer, (int, float)) and buffer > 0:
                self.microphone.retarget(buffer)
        elif kind == Packet.SOUND and self._decoder is not None:
            data = packet.get(Field.DATA)
            if isinstance(data, bytes):
                try:
                    self.microphone.put(self._decoder.decode(data, FRAME))
                except opuslib.OpusError:
                    logger.warning("A microphone frame from client %s did not decode.", self.client_id or "?")

    def open_microphone(self, wanted: bool) -> None:
        if wanted == self.microphone_open:
            return
        self.microphone_open = wanted
        if not wanted:
            self.microphone.clear()
        self.send({Field.CMD: Packet.MICROPHONE, Field.DATA: wanted})


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
        self._encoder = None
        self._changed = anyio.Event()
        self._scope: anyio.CancelScope | None = None
        #: Set once PipeWire listens, for whoever waits on it. A test does.
        self.running = anyio.Event()

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
        old = self.links.get(link.client_id)
        self.links[link.client_id] = link
        logger.info("Client %s carries sound.", link.client_id or "?")
        if old is not None and old is not link:
            old.connection.detach_and_close()
        self._tell(link)
        self._steer_microphone()

    def link_closed(self, link: AudioLink) -> None:
        if self.links.get(link.client_id) is link:
            del self.links[link.client_id]
        self._steer_microphone()

    def client_options_changed(self) -> None:
        "A client's `audio` or `audio-rtt-multiplier` moved."
        for link in self.links.values():
            self._tell(link)
        self._steer_microphone()

    def _client_of(self, link: AudioLink):
        for client_state in self.pymux.clients:
            if getattr(client_state.connection, "client_id", None) == link.client_id:
                return client_state
        return None

    def _audio_of(self, link: AudioLink) -> ClientAudio:
        client_state = self._client_of(link)
        return client_state.audio if client_state is not None else ClientAudio.ON

    def _tell(self, link: AudioLink) -> None:
        client_state = self._client_of(link)
        if client_state is None:
            return
        link.send(
            {
                Field.CMD: Packet.AUDIO,
                Field.DATA: str(client_state.audio),
                Field.RTT_MULTIPLIER: client_state.audio_rtt_multiplier,
            }
        )

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
            link.open_microphone(self.recording and lends and link is current)

    # ------------------------------------------------------------------
    # Running.

    def option_changed(self) -> None:
        "`audio-server` moved: stop, and start again as it says."
        self._changed.set()
        if self._scope is not None:
            self._scope.cancel()

    async def keep(self) -> None:
        "Run PipeWire whenever the option allows. Never returns."
        while True:
            have = programs()
            self.mode = resolve(self.pymux.audio_server, have)
            if self.mode != AudioServerMode.OFF:
                with anyio.CancelScope() as self._scope:
                    try:
                        await self._serve(have)
                    except Exception:
                        logger.exception("Sound stopped.")
                self._scope = None
                self.mode = AudioServerMode.OFF
            await self._changed.wait()
            self._changed = anyio.Event()

    async def _serve(self, have: dict[str, str]) -> None:
        speaker = await anyio.create_udp_socket(local_host="127.0.0.1", local_port=0)
        microphone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        microphone.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        microphone.bind(("127.0.0.1", 0))
        microphone_port = microphone.getsockname()[1]
        sender = await anyio.abc.UDPSocket.from_socket(microphone)

        if self.mode == AudioServerMode.SHARED:
            suffix = "-%d" % (os.getpid(),)
            self.speaker_node, self.microphone_node = "pymux-speaker" + suffix, "pymux-microphone" + suffix
        else:
            self.speaker_node, self.microphone_node = "pymux-speaker", "pymux-microphone"
        rtp = [rtp_sink(speaker.extra(anyio.abc.SocketAttribute.local_port), self.speaker_node)]
        rtp.append(rtp_source(microphone_port, self.microphone_node))

        runtime = runtime_dir_of(self.pymux.socket_name) if self.mode == AudioServerMode.PRIVATE else None
        work = tempfile.mkdtemp(prefix="pymux-audio-conf-")
        self._encoder = opuslib.Encoder(RATE, SPEAKER_CHANNELS, opuslib.APPLICATION_AUDIO)
        self._encoder.bitrate = SPEAKER_BITRATE
        try:
            async with speaker, sender, anyio.create_task_group() as tasks:
                if runtime is not None:
                    os.makedirs(runtime, mode=0o700, exist_ok=True)
                    await self._start_private(tasks, have, runtime, work, rtp)
                    self.runtime = runtime
                else:
                    await self._start_host(tasks, have, work, rtp)
                tasks.start_soon(self._speak, speaker)
                tasks.start_soon(self._listen, sender, microphone_port)
                tasks.start_soon(self._watch, microphone_port)
                logger.info("Sound on (%s PipeWire): %s and %s.", self.mode, self.speaker_node, self.microphone_node)
                self.running.set()
        finally:
            self.running = anyio.Event()
            self.runtime = None
            self.recording = False
            self._steer_microphone()
            shutil.rmtree(work, ignore_errors=True)

    async def _start_private(
        self, tasks: anyio.abc.TaskGroup, have: dict[str, str], runtime: str, work: str, rtp: list
    ) -> None:
        conf = os.path.join(work, "pipewire.conf")
        with open(conf, "w") as out:
            write_config(daemon_config(runtime, rtp), out)
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
        for leftover in ("pipewire-0", "pipewire-0.lock", "pipewire-0-manager", "pipewire-0-manager.lock", "pulse-native"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(runtime, leftover))
        tasks.start_soon(self._run, [have["pipewire"], "-c", conf], environment, "pipewire")
        with anyio.fail_after(10):
            while not os.path.exists(os.path.join(runtime, "pipewire-0")):
                await anyio.sleep(0.05)
        tasks.start_soon(self._run, [have["wireplumber"], "-p", "pymux"], environment, "wireplumber")

    async def _start_host(self, tasks: anyio.abc.TaskGroup, have: dict[str, str], work: str, rtp: list) -> None:
        conf = os.path.join(work, "pymux-audio.conf")
        with open(conf, "w") as out:
            write_config(host_config(rtp), out)
        tasks.start_soon(self._run, [have["pipewire"], "-c", conf], dict(os.environ), "pipewire")

    async def _run(self, argv: list[str], environment: dict[str, str], name: str) -> None:
        "Run one program for as long as sound is on. One that ends ends sound."
        process = await anyio.open_process(argv, env=environment, stdin=DEVNULL, stdout=DEVNULL, stderr=None)
        try:
            code = await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(2, shield=True):
                    await process.wait()
                if process.returncode is None:
                    process.kill()
        raise RuntimeError("%s ended with %s" % (name, code))

    async def _speak(self, speaker: anyio.abc.UDPSocket) -> None:
        "What panes play, from PipeWire's RTP to the current client as Opus."
        held = bytearray()
        whole = FRAME * SPEAKER_CHANNELS * 2
        while True:
            datagram, _address = await speaker.receive()
            link = self.current()
            if link is None or len(datagram) < _RTP.size:
                held.clear()
                continue
            held += datagram[_RTP.size + 4 * (datagram[0] & 0x0F) :]
            while len(held) >= whole:
                pcm = l16_to_native(bytes(held[:whole]))
                del held[:whole]
                link.send(
                    {
                        Field.CMD: Packet.SOUND,
                        Field.DATA: self._encoder.encode(pcm, FRAME),
                        Field.CHANNELS: SPEAKER_CHANNELS,
                    }
                )

    async def _listen(self, sender: anyio.abc.UDPSocket, port: int) -> None:
        "The current client's microphone, paced into PipeWire every 20 ms."
        ssrc = int.from_bytes(os.urandom(4), "big")
        sequence = 0
        timestamp = 0
        deadline = anyio.current_time()
        while True:
            deadline += FRAME_MS / 1000
            await anyio.sleep_until(deadline)
            timestamp = (timestamp + FRAME) & 0xFFFFFFFF
            # The client used last can change between any two frames.
            self._steer_microphone()
            link = self.current()
            frame = link.microphone.take() if link is not None and link.microphone_open else None
            if frame is None:
                continue
            header = _RTP.pack(0x80, L16_PAYLOAD, sequence, timestamp, ssrc)
            sequence = (sequence + 1) & 0xFFFF
            await sender.sendto(header + native_to_l16(frame), "127.0.0.1", port)

    async def _watch(self, port: int) -> None:
        """
        Whether a program records from the microphone.

        `module-rtp-source` opens its socket when a program starts to
        read from it and closes it when the last one stops. So the
        socket table says it, passively, in either mode.
        `pw-dump --monitor` would say it too, and hangs on a system-wide
        PipeWire whose manager socket the user cannot open.
        """
        while True:
            recording = await anyio.to_thread.run_sync(_listens_for, port)
            if recording != self.recording:
                self.recording = recording
                logger.info("A program %s the microphone.", "records from" if recording else "stopped recording from")
                self._steer_microphone()
            await anyio.sleep(WATCH_EVERY)
