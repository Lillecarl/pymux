"""
The parts of sound that need no PipeWire: the jitter buffer, the round
trip, which mode a server runs, and which client hears it.
`tests/test_audio.py` runs them against a real PipeWire.
Lillecarl/pymux#657.
"""

from __future__ import annotations

import json

import pytest

from pymux import audio
from pymux.audio import (
    MICROPHONE_OFF,
    MICROPHONE_ON,
    MICROPHONE_SILENT,
    AudioServer,
    daemon_config,
    resolve,
    rtp_sink,
    rtp_source,
)
from pymux.enums import AudioServerMode, ClientAudio
from pymux.jitter import CEILING_MS, FLOOR_MS, FRAME_MS, SLACK, JitterBuffer, RoundTrip
from pymux.main import Pymux

PIPEWIRE = {"pipewire": "/bin/pipewire", "wireplumber": "/bin/wireplumber"}


def frame(n: int) -> bytes:
    return bytes([n]) * 4


def test_the_buffer_plays_only_once_it_holds_its_target():
    buffer = JitterBuffer(3 * FRAME_MS)
    buffer.put(frame(1))
    buffer.put(frame(2))
    assert buffer.take() is None
    buffer.put(frame(3))
    assert [buffer.take(), buffer.take(), buffer.take()] == [frame(1), frame(2), frame(3)]


def test_an_empty_buffer_fills_again_before_it_plays():
    "One frame at a time after a gap would stutter."
    buffer = JitterBuffer(2 * FRAME_MS)
    for n in range(2):
        buffer.put(frame(n))
    buffer.take()
    buffer.take()
    assert buffer.take() is None
    assert buffer.underruns == 1
    buffer.put(frame(9))
    assert buffer.take() is None


def test_a_burst_past_the_target_drops_the_oldest():
    buffer = JitterBuffer(2 * FRAME_MS)
    for n in range(10):
        buffer.put(frame(n))
    assert len(buffer) == 2 + SLACK
    assert buffer.take() == frame(10 - 2 - SLACK)


def test_a_smaller_target_trims_at_once():
    buffer = JitterBuffer(10 * FRAME_MS)
    for n in range(10):
        buffer.put(frame(n))
    buffer.retarget(FRAME_MS)
    assert len(buffer) == 1 + SLACK
    assert buffer.target_ms == FRAME_MS


def test_the_round_trip_sizes_the_buffer_by_its_slowest():
    trips = RoundTrip()
    assert trips.buffer_ms(2.0) == FLOOR_MS
    for seconds in (0.030, 0.050, 0.040):
        trips.heard(seconds)
    assert trips.buffer_ms(2.0) == pytest.approx(100)
    assert trips.buffer_ms(0.1) == FLOOR_MS
    trips.heard(5.0)
    assert trips.buffer_ms(2.0) == CEILING_MS


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
    config = daemon_config("/run/x", [rtp_sink(4000, "s"), rtp_source(4001, "m")])
    names = [module["name"] for module in json.loads(json.dumps(config))["context.modules"]]
    assert "libpipewire-module-access" in names
    assert names[-2:] == ["libpipewire-module-rtp-sink", "libpipewire-module-rtp-source"]


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
        self.message = ""


class _Link:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.sent: list = []
        self.microphone_open = False
        self.multiplier: float | None = None

    def retarget(self, multiplier: float | None = None) -> None:
        self.multiplier = multiplier

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
    assert links["a"].sent[-1] == {"cmd": "audio", "data": "on"}
    assert links["a"].multiplier == 3.5


def test_the_options_are_there():
    pymux = Pymux()
    assert pymux.audio_server == AudioServerMode.AUTO
    pymux.handle_command("set-option audio-server off")
    assert pymux.audio_server == AudioServerMode.OFF
