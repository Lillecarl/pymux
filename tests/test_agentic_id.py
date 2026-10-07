"""
What a detached command says about who sent it.

The client resolves the agent session id from its own environment --
fresh on every invocation -- and sends it under `PYMUX_AGENTIC_ID`
inside the `environment` field of the `run-command` packet
(`client/agentic.py`). These tests read what such a packet carries,
without a server: a stub socket replays the answer, and the captured
send is what the wire would have held.
"""

from __future__ import annotations

import json

from libpymux.protocol import Field, Packet

from pymux.client.agentic import AGENT_SESSION_VARS, PYMUX_AGENTIC_ID, caller_environment
from pymux.client.posix import PosixClient

# ----------------------------------------------------------------------
# The resolution.


def test_first_set_variable_wins():
    env = {"OC_SESSION": "oc-1", "CODEX_THREAD_ID": "codex-1"}
    assert caller_environment(env) == {PYMUX_AGENTIC_ID: "oc-1"}


def test_first_party_variables_come_before_third_party_ones():
    env = {"CURSOR_TRACE_ID": "cursor-1", "OPENCODE_SESSION_ID": "opencode-1"}
    assert caller_environment(env) == {PYMUX_AGENTIC_ID: "opencode-1"}


def test_empty_values_do_not_count():
    env = {"OPENCODE_SESSION_ID": "", "OCAHUB_SESSION": "ocahub-1"}
    assert caller_environment(env) == {PYMUX_AGENTIC_ID: "ocahub-1"}


def test_an_explicit_value_passes_straight_through():
    env = {PYMUX_AGENTIC_ID: "pinned-1", "OPENCODE_SESSION_ID": "opencode-1"}
    assert caller_environment(env) == {PYMUX_AGENTIC_ID: "pinned-1"}


def test_no_variables_means_anonymous():
    assert caller_environment({}) == {}


def test_every_known_variable_resolves():
    for var in AGENT_SESSION_VARS:
        assert caller_environment({var: "s"}) == {PYMUX_AGENTIC_ID: "s"}


# ----------------------------------------------------------------------
# What crosses the wire.


class StubSocket:
    """A socket that replays one answer and keeps what was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = b""

    def setblocking(self, _flag):
        pass

    def send(self, data):
        self.sent += data
        return len(data)

    def recv(self, _size):
        if self.answers:
            return self.answers.pop(0)
        return b""


def run_command_packet(monkeypatch, command="show-job", **env):
    """
    The `run-command` packet a detached command sends, with `env` as
    the whole client environment.
    """
    for var in AGENT_SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)

    exit_packet = json.dumps({Field.CMD: Packet.EXIT, Field.CODE: 0}).encode() + b"\0"
    client = PosixClient.__new__(PosixClient)
    client.socket = StubSocket(exit_packet)
    assert client.run_command(command) == 0
    # Packets are `\0`-terminated on the wire; the first one is the command.
    sent, _terminator, _rest = client.socket.sent.partition(b"\0")
    return json.loads(sent.decode())


def test_packet_carries_the_resolved_id(monkeypatch):
    packet = run_command_packet(monkeypatch, OPENCODE_SESSION_ID="opencode-1")
    assert packet[Field.CMD] == Packet.RUN_COMMAND
    assert packet[Field.DATA] == "show-job"
    assert packet[Field.ENVIRONMENT] == {PYMUX_AGENTIC_ID: "opencode-1"}


def test_packet_carries_nothing_when_anonymous(monkeypatch):
    packet = run_command_packet(monkeypatch)
    assert packet[Field.ENVIRONMENT] == {}
