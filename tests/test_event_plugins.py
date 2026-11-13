"""
Event plugins: a module names a `Plugin`, the server runs it, and it
hears what happens. Lillecarl/pyterm#982.
"""

from __future__ import annotations

import shlex
import sys
from types import SimpleNamespace

import anyio
from prompt_toolkit.application.current import set_app
from pyte.osc import Osc
from pyte.program_status import parse_report
from run_commands import answered
from session import create_session

from pymux import commands
from pymux.core.main import Pymux
from pymux.ids import PaneId
from pymux.mirror.program import RemoteProgram
from pymux.plugin_api import BellRang, CommandFinished, Notified, PaneEnded, Plugin, ProgramReported
from pymux.plugin_api.runner import QUEUE_LENGTH, Plugins, PluginState, Slot, handled

DEADLINE = 10

SLEEPS = "%s -c %s" % (sys.executable, shlex.quote("import time; time.sleep(60)"))
RINGS = "%s -c %s" % (sys.executable, shlex.quote("import sys; sys.stdout.write(chr(7))"))

#: Keeps every event it is given.
RECORDER = """
from pymux.plugin_api import Plugin


class Recorder(Plugin):
    def __init__(self, host):
        super().__init__(host)
        self.heard = []

    async def on_notified(self, event):
        self.heard.append(event)

    async def on_program_reported(self, event):
        self.heard.append(event)

    async def on_command_finished(self, event):
        self.heard.append(event)

    async def on_bell(self, event):
        self.heard.append(event)

    async def on_pane_ended(self, event):
        self.heard.append(event)


plugin = Recorder
"""

RAISES_ON_AN_EVENT = """
from pymux.plugin_api import Plugin


class Broken(Plugin):
    async def on_notified(self, event):
        raise RuntimeError("this plugin is broken")


plugin = Broken
"""

RAISES_WHEN_MADE = """
def plugin(host):
    raise RuntimeError("this plugin cannot start")
"""

ASKS_OVER_THE_SOCKET = """
from pymux.plugin_api import Plugin


class Asks(Plugin):
    answer = None

    async def run(self):
        self.answer = await self.host.command("show-plugins")


plugin = Asks
"""


def _home(tmp_path, monkeypatch, **found):
    directory = tmp_path / "pymux" / "plugins"
    directory.mkdir(parents=True)
    for name, text in found.items():
        (directory / ("%s.py" % name)).write_text(text)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(commands, "_parser_tree", None)


async def _until(condition) -> None:
    while not condition():
        await anyio.sleep(0.01)


async def _plugin(pymux: Pymux, name: str) -> Plugin:
    await _until(lambda: name in pymux.plugins.slots and pymux.plugins.slots[name].plugin is not None)
    return pymux.plugins.slots[name].plugin


async def test_a_plugin_hears_what_happens_in_its_server(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, hears_its_server=RECORDER)
    with anyio.fail_after(DEADLINE):
        async with create_session(window=SLEEPS) as (pymux, state):
            recorder = await _plugin(pymux, "hears_its_server")
            pane = pymux.arrangement.get_active_pane_for(state.app)

            record = pymux.notification_center.add("build", "done", pane_id=pane.pane_id)
            pymux.outward.forward_osc(pane, Osc.PROGRAM_STATUS, "state=blocked:kind=permission")
            pymux.outward.forward_osc(pane, Osc.SHELL_INTEGRATION, "D;3")
            with set_app(state.app):
                (rings,) = (await pymux.panes.create_window(RINGS)).panes

            await _until(lambda: PaneEnded(rings.pane_id) in recorder.heard)
            # Before the session ends, and its own pane with it.
            heard = list(recorder.heard)

        assert heard == [
            Notified(record),
            ProgramReported(pane.pane_id, parse_report("state=blocked:kind=permission")),
            CommandFinished(pane.pane_id, 3),
            BellRang(rings.pane_id),
            PaneEnded(rings.pane_id),
        ]


def _notified(recorder) -> list[Notified]:
    "The window of `create_session` runs a program that ends, and that is heard too."
    return [event for event in recorder.heard if isinstance(event, Notified)]


async def test_a_plugin_that_raises_fails_alone(tmp_path, monkeypatch):
    "In the server's task group it would cancel every client."
    _home(
        tmp_path,
        monkeypatch,
        alone_raises_on_an_event=RAISES_ON_AN_EVENT,
        alone_raises_when_made=RAISES_WHEN_MADE,
        alone_records=RECORDER,
    )
    with anyio.fail_after(DEADLINE):
        async with create_session() as (pymux, state):
            recorder = await _plugin(pymux, "alone_records")
            slots = pymux.plugins.slots

            pymux.notification_center.add("first")
            await _until(lambda: slots["alone_raises_on_an_event"].state is PluginState.FAILED)
            pymux.notification_center.add("second")
            await _until(lambda: len(_notified(recorder)) == 2)

            listing = (await answered(pymux, state, "show-plugins")).output

        assert [event.notification.title for event in _notified(recorder)] == ["first", "second"]
        assert "\n".join(listing).splitlines() == [
            "alone_raises_on_an_event: failed, RuntimeError: this plugin is broken",
            "alone_raises_when_made: failed, RuntimeError: this plugin cannot start",
            "alone_records: running",
        ]


async def test_a_plugin_runs_a_command_through_its_host(tmp_path, monkeypatch):
    "Over the server's own socket, as `pymux show-plugins` typed in a pane."
    _home(tmp_path, monkeypatch, asks_over_the_socket=ASKS_OVER_THE_SOCKET)
    pymux = Pymux()
    pymux.serving.listen_on_socket(str(tmp_path / "socket"))
    with anyio.fail_after(DEADLINE):
        async with create_session(pymux) as (pymux, state):
            asks = await _plugin(pymux, "asks_over_the_socket")
            await _until(lambda: asks.answer is not None)
            pane = pymux.arrangement.get_active_pane_for(state.app)
            # The wire takes the pane's number; libpymux names it `%N`.
            from_the_pane = await asks.host.command("display-message -p '#{pane_id}'", pane.pane_id)

    assert (asks.answer.stdout, asks.answer.exit_code) == ("asks_over_the_socket: running\n", 0)
    assert (from_the_pane.stdout, from_the_pane.exit_code) == ("%%%d\n" % pane.pane_id, 0)


def test_only_the_events_a_plugin_hears_are_queued_for_it():
    class Bells(Plugin):
        async def on_bell(self, event):
            pass

    assert handled(Bells) == {BellRang}
    assert handled(Plugin) == frozenset()


def test_a_mirrored_pane_is_heard_where_it_runs():
    "The server that owns the pane emits it too, and one bell would wake two plugins."
    plugins = Plugins(Pymux())
    send, receive = anyio.create_memory_object_stream[BellRang](QUEUE_LENGTH)
    plugins.slots["hears"] = Slot("hears", send, receive, handles=frozenset({BellRang}))
    here = SimpleNamespace(process=None)
    mirror = SimpleNamespace(process=RemoteProgram.__new__(RemoteProgram))

    plugins.emit(BellRang(PaneId(1)), mirror)
    plugins.emit(BellRang(PaneId(2)), here)

    assert receive.receive_nowait() == BellRang(PaneId(2))
    assert receive.statistics().current_buffer_used == 0
    send.close()
    receive.close()


def test_a_plugin_that_falls_behind_loses_the_oldest():
    "The newest says how things are now."
    send, receive = anyio.create_memory_object_stream[BellRang](QUEUE_LENGTH)
    slot = Slot("behind", send, receive)

    for number in range(QUEUE_LENGTH + 2):
        slot.offer(BellRang(number))

    assert slot.dropped == 2
    assert receive.receive_nowait() == BellRang(2)
    send.close()
    receive.close()
