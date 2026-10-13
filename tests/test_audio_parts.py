"""
The parts of sound that need no PipeWire: the jitter buffer, the round
trip, which mode a server runs, and which client hears it.
`tests/test_audio.py` runs them against a real PipeWire.
Lillecarl/pymux#657.
"""

from __future__ import annotations

import json
import math

import pytest

from pymux import audio
from pymux.audio import (
    MICROPHONE_OFF,
    MICROPHONE_ON,
    MICROPHONE_SILENT,
    AudioServer,
    daemon_config,
    pw_cat,
    resolve,
)
from pymux.enums import AudioFrame, AudioServerMode, ClientAudio
from pymux.jitter import (
    CEILING_MS,
    DEFAULT_FRAME_MS,
    FRAME_SIZES,
    SLACK_FRAMES,
    SLACK_MS,
    JitterBuffer,
    RoundTrip,
    floor_ms,
    samples_per_frame,
    stats_every,
)
from pymux.main import Pymux

PIPEWIRE = {"pipewire": "/bin/pipewire", "wireplumber": "/bin/wireplumber"}

#: The frame the buffer tests take, and the frames its cap holds past the target.
F = DEFAULT_FRAME_MS
SLACK = max(SLACK_FRAMES, math.ceil(SLACK_MS / F))
FLOOR = floor_ms(F)


def frame(n: int, frame_ms: float = F) -> bytes:
    "A frame of mono sound, every byte `n`."
    return bytes([n]) * (samples_per_frame(frame_ms) * 2)


def test_the_buffer_plays_only_once_it_holds_its_target():
    buffer = JitterBuffer(3 * F)
    buffer.put(frame(1))
    buffer.put(frame(2))
    assert buffer.take() is None
    buffer.put(frame(3))
    assert buffer.take() == frame(1)


@pytest.mark.parametrize("frame_ms", FRAME_SIZES)
def test_every_opus_frame_size_follows_a_fast_clock(frame_ms):
    """
    200 ppm fast, at every frame size Opus has: absorbed a sample at a
    time, with no frame lost. One sample a frame follows 350 ppm at
    60 ms frames; real clocks drift under a hundred.
    """
    buffer = JitterBuffer(3 * frame_ms, frame_ms=frame_ms)
    for _ in range(3):
        buffer.put(frame(1, frame_ms))
    ticks = round(1_000_000 / frame_ms)
    for tick in range(1, ticks + 1):
        assert len(buffer.take() or b"") in (0, len(frame(1, frame_ms)))
        buffer.put(frame(1, frame_ms))
        if tick % 5000 == 0:
            buffer.put(frame(1, frame_ms))
    assert buffer.squeezed > 0
    assert (buffer.dropped, buffer.underruns) == (0, 0)


def test_a_new_frame_size_keeps_the_buffer_it_was_asked_for():
    buffer = JitterBuffer(60, frame_ms=20)
    buffer.reframe(2.5)
    assert (buffer.frame_ms, buffer.target_ms) == (2.5, 60)
    # Two frames are the floor: two of 60 ms are 120.
    buffer.reframe(60)
    assert buffer.target_ms == 120


def test_a_frame_taken_is_always_one_frame_long():
    "A speaker's callback takes 960 samples, whatever the buffer does to follow the clock."
    buffer = JitterBuffer(2 * F)
    for n in range(5):
        buffer.put(frame(n))
    sizes = set()
    while (taken := buffer.take()) is not None:
        sizes.add(len(taken))
    assert sizes == {len(frame(0))}


def follow(buffer: JitterBuffer, ticks: int, extra_every: int = 0, missing_every: int = 0) -> None:
    "A far clock against the speaker's: one frame made and one taken a tick, and one more or one fewer now and then."
    for _ in range(buffer._target):
        buffer.put(frame(1))
    for tick in range(1, ticks + 1):
        buffer.take()
        if not (missing_every and tick % missing_every == 0):
            buffer.put(frame(1))
        if extra_every and tick % extra_every == 0:
            buffer.put(frame(1))


def test_a_far_clock_that_runs_fast_is_followed_by_dropping_samples():
    "500 ppm fast: what it gains is dropped a sample at a time, never a frame."
    buffer = JitterBuffer(3 * F)
    follow(buffer, 20000, extra_every=2000)
    assert buffer.squeezed > 0
    assert (buffer.dropped, buffer.underruns) == (0, 0)
    assert len(buffer) <= 3 + SLACK


def test_a_far_clock_that_runs_slow_is_followed_by_repeating_samples():
    "500 ppm slow: the speaker never runs dry."
    buffer = JitterBuffer(3 * F)
    follow(buffer, 20000, missing_every=2000)
    assert buffer.stretched > 0
    assert (buffer.dropped, buffer.underruns) == (0, 0)


def test_a_fill_that_sits_a_little_high_is_not_corrected():
    """
    Off a real network frames arrive unevenly, and a steady buffer sits a
    frame or so over its target. From the MacBook it sat a frame and a
    quarter over, and the buffer squeezed every frame chasing it.
    """
    buffer = JitterBuffer(2 * F)
    for _ in range(3):
        buffer.put(frame(1))
    for _ in range(5000):
        buffer.take()
        buffer.put(frame(1))
    assert (buffer.squeezed, buffer.stretched) == (0, 0)


def test_clocks_that_agree_are_left_alone():
    buffer = JitterBuffer(3 * F)
    follow(buffer, 5000)
    assert (buffer.squeezed, buffer.stretched, buffer.dropped, buffer.underruns) == (0, 0, 0, 0)


def test_an_empty_buffer_fills_again_before_it_plays():
    "One frame at a time after a gap would stutter."
    buffer = JitterBuffer(2 * F)
    for n in range(2):
        buffer.put(frame(n))
    buffer.take()
    buffer.take()
    # A sample or two short is still a frame, by repeating one; a frame
    # short is a gap.
    assert buffer.take() is None
    assert buffer.underruns == 1
    buffer.put(frame(9))
    assert buffer.take() is None


def test_a_burst_past_the_target_drops_the_oldest():
    buffer = JitterBuffer(2 * F)
    for n in range(30):
        buffer.put(frame(n))
    assert len(buffer) == 2 + SLACK
    assert buffer.take()[:2] == frame(30 - 2 - SLACK)[:2]


def test_a_smaller_target_trims_at_once():
    buffer = JitterBuffer(10 * F)
    for n in range(20):
        buffer.put(frame(n))
    buffer.retarget(F)
    assert len(buffer) == 1 + SLACK
    assert buffer.target_ms == F


def test_stats_are_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("PYMUX_AUDIO_STATS", raising=False)
    assert stats_every() is None
    monkeypatch.setenv("PYMUX_AUDIO_STATS", "5")
    assert stats_every() == 5.0
    for wrong in ("0", "-1", "often"):
        monkeypatch.setenv("PYMUX_AUDIO_STATS", wrong)
        assert stats_every() is None


def test_a_buffer_describes_itself_for_a_stats_line():
    buffer = JitterBuffer(3 * F)
    buffer.put(frame(1))
    assert buffer.describe() == "frames of 10 ms, holds 10/30 ms, underruns 0, dropped 0, squeezed 0, stretched 0"


def test_the_round_trip_reads_as_fastest_median_slowest():
    trips = RoundTrip()
    assert trips.describe() == "unmeasured"
    for seconds in (0.030, 0.010, 0.020):
        trips.heard(seconds)
    assert trips.describe() == "10.0/20.0/30.0"


def test_the_round_trip_sizes_the_buffer_by_its_slowest():
    trips = RoundTrip()
    assert trips.buffer_ms(2.0, FLOOR) == FLOOR
    for seconds in (0.030, 0.050, 0.040):
        trips.heard(seconds)
    assert trips.buffer_ms(2.0, FLOOR) == pytest.approx(80)
    assert trips.buffer_ms(0.1, FLOOR) == FLOOR
    # One slow echo alone moves nothing; a second does.
    trips.heard(5.0)
    assert trips.buffer_ms(2.0, FLOOR) == pytest.approx(100)
    trips.heard(5.0)
    assert trips.buffer_ms(2.0, FLOOR) == CEILING_MS


@pytest.fixture
def carried(monkeypatch):
    def carry(yes: bool) -> None:
        monkeypatch.setattr(audio, "carried", lambda: yes)

    return carry


def test_auto_is_private_when_the_build_carries_pipewire(carried, monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "linux")
    carried(True)
    assert resolve(AudioServerMode.AUTO, PIPEWIRE) == AudioServerMode.PRIVATE
    carried(False)
    assert resolve(AudioServerMode.AUTO, PIPEWIRE) == AudioServerMode.SHARED


def test_no_pipewire_is_no_sound(carried, monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "linux")
    carried(True)
    for mode in AudioServerMode:
        assert resolve(mode, {}) == AudioServerMode.OFF
    assert resolve(AudioServerMode.PRIVATE, {"pipewire": "/bin/pipewire"}) == AudioServerMode.OFF
    assert resolve(AudioServerMode.SHARED, {"pipewire": "/bin/pipewire"}) == AudioServerMode.SHARED


def test_an_integrated_server_has_no_sound_of_its_own(carried, monkeypatch):
    "Its panes are on the person's machine already, and play on its sound."
    monkeypatch.setattr(audio.sys, "platform", "linux")
    carried(True)
    for mode in AudioServerMode:
        assert resolve(mode, PIPEWIRE, serves_one_terminal=True) == AudioServerMode.OFF


def test_macos_has_no_server_sound(carried, monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    carried(True)
    assert resolve(AudioServerMode.PRIVATE, PIPEWIRE) == AudioServerMode.OFF


def test_the_config_is_json_pipewire_reads():
    "SPA-JSON reads JSON, so the config is written with `json` and nothing is quoted by hand."
    config = daemon_config("/run/x")
    names = [module["name"] for module in json.loads(json.dumps(config))["context.modules"]]
    assert "libpipewire-module-access" in names
    assert "libpipewire-module-protocol-pulse" in names


def test_a_device_is_a_pw_cat_that_names_its_node():
    argv = pw_cat("/bin/pw-cat", "--record", 2, "pymux-speaker", "the speaker", "Audio/Sink", 2000)
    assert argv[:2] == ["/bin/pw-cat", "--record"]
    properties = json.loads(argv[argv.index("-P") + 1])
    assert properties == {
        "media.class": "Audio/Sink",
        "node.name": "pymux-speaker",
        "node.description": "the speaker",
        "priority.session": 2000,
    }
    assert argv[-1] == "-"


def test_panes_find_the_private_pipewire():
    server = AudioServer(Pymux())
    server.mode = AudioServerMode.PRIVATE
    server.runtime = "/run/pymux/audio-x"
    environment: dict[str, str] = {}
    server.pane_environment(environment)
    assert environment == {
        "PIPEWIRE_REMOTE": "/run/pymux/audio-x/pipewire-0",
        "PULSE_SERVER": "unix:/run/pymux/audio-x/pulse-native",
    }


def test_panes_name_the_shared_speaker():
    server = AudioServer(Pymux())
    server.mode = AudioServerMode.SHARED
    server.speaker_node, server.microphone_node = "pymux-speaker-7", "pymux-microphone-7"
    environment: dict[str, str] = {}
    server.pane_environment(environment)
    assert environment == {"PULSE_SINK": "pymux-speaker-7", "PULSE_SOURCE": "pymux-microphone-7"}


def test_panes_get_nothing_while_sound_is_off():
    server = AudioServer(Pymux())
    environment: dict[str, str] = {}
    server.pane_environment(environment)
    assert environment == {}


class _Connection:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id


class _Client:
    def __init__(self, client_id: str, last_used: int, mode: ClientAudio = ClientAudio.ON) -> None:
        self.connection = _Connection(client_id)
        self.last_used = last_used
        self.audio = mode
        self.audio_rtt_multiplier = 2.0
        self.audio_frame = AudioFrame.MS_10
        self.message = ""


class _Link:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.sent: list = []
        self.microphone_open = False
        self.multiplier: float | None = None

    def retarget(self, multiplier: float | None = None) -> None:
        self.multiplier = multiplier

    def reframe(self, frame_ms: float) -> None:
        self.frame_ms = frame_ms

    def send(self, packet) -> None:
        self.sent.append(packet)

    def open_microphone(self, wanted: bool) -> bool:
        changed = wanted != self.microphone_open
        self.microphone_open = wanted
        return changed


@pytest.fixture
def routed(monkeypatch):
    pymux = Pymux()
    clients = [_Client("a", 1), _Client("b", 2)]
    monkeypatch.setattr(type(pymux), "clients", property(lambda self: clients))
    server = AudioServer(pymux)
    links = {name: _Link(name) for name in ("a", "b")}
    server.links.update(links)
    return server, clients, links


def test_sound_goes_to_the_client_used_last(routed):
    server, clients, links = routed
    assert server.current() is links["b"]
    clients[0].last_used = 3
    assert server.current() is links["a"]


def test_a_client_with_audio_off_is_never_it(routed):
    server, clients, links = routed
    clients[1].audio = ClientAudio.OFF
    assert server.current() is links["a"]


def test_the_microphone_opens_only_for_a_client_that_lends_it(routed):
    server, clients, links = routed
    server.recording = True
    server._steer_microphone()
    assert links["b"].microphone_open and not links["a"].microphone_open
    clients[1].audio = ClientAudio.PLAYBACK
    server._steer_microphone()
    assert not links["b"].microphone_open


def test_the_person_is_told_when_their_microphone_opens(routed):
    server, clients, links = routed
    server.recording = True
    server._steer_microphone()
    assert clients[1].message == MICROPHONE_ON
    assert clients[0].message == ""
    server.recording = False
    server._steer_microphone()
    assert clients[1].message == MICROPHONE_OFF


def test_the_person_is_told_when_their_microphone_is_refused(routed):
    server, clients, links = routed
    server.microphone_silent(links["b"])
    assert clients[1].message == MICROPHONE_SILENT


def test_a_client_hears_its_options(routed):
    server, clients, links = routed
    clients[0].audio_rtt_multiplier = 3.5
    server.client_options_changed()
    assert links["a"].sent[-1] == {"cmd": "audio", "data": "on", "frame": 10.0}
    assert links["a"].multiplier == 3.5


def test_the_options_are_there():
    pymux = Pymux()
    assert pymux.audio_server == AudioServerMode.AUTO
    pymux.handle_command("set-option audio-server off")
    assert pymux.audio_server == AudioServerMode.OFF
