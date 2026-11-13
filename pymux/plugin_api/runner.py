"""
The server's side of event plugins: it finds them, runs each in a task
of its own, and hands each the events it asked for.

`emit` runs where the event happens -- a pane's read, a frame, a
command -- so it never awaits, never raises and never waits on a
plugin. It puts the event in the plugin's own queue, and the plugin's
task takes it from there. Lillecarl/pyterm#982.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from ..log import logger
from ..mirror.program import RemoteProgram
from .events import EVENTS, Event
from .plugin import Host, Plugin

if TYPE_CHECKING:
    from ..core.main import Pymux
    from ..core.pane import Pane

__all__ = ["QUEUE_LENGTH", "PluginState", "Plugins", "Slot"]

#: How many events wait for one plugin. A plugin that falls further
#: behind loses the oldest: the newest says how things are now.
QUEUE_LENGTH = 256


class PluginState(StrEnum):
    RUNNING = "running"
    #: It raised; `Slot.error` says what. It hears nothing more until
    #: the server starts again.
    FAILED = "failed"
    #: The server's scope ended, and the plugin with it.
    STOPPED = "stopped"


@dataclass(eq=False)
class Slot:
    "One plugin, as the server runs it."

    name: str
    send: MemoryObjectSendStream[Event]
    receive: MemoryObjectReceiveStream[Event]
    #: None until its factory returned.
    plugin: Plugin | None = None
    handles: frozenset[type[Event]] = frozenset()
    state: PluginState = PluginState.RUNNING
    #: Events lost because the plugin fell `QUEUE_LENGTH` behind.
    dropped: int = 0
    error: str | None = None

    def offer(self, event: Event) -> None:
        try:
            self.send.send_nowait(event)
        except anyio.WouldBlock:
            self.receive.receive_nowait()
            self.dropped += 1
            self.send.send_nowait(event)


def handled(plugin: type[Plugin]) -> frozenset[type[Event]]:
    "The events whose method this plugin class overrides."
    return frozenset(event for event in EVENTS if getattr(plugin, event.HANDLER) is not getattr(Plugin, event.HANDLER))


@dataclass(eq=False)
class Plugins:
    "Every event plugin of one server, by name."

    pymux: Pymux
    slots: dict[str, Slot] = field(default_factory=dict)

    def emit(self, event: Event, pane: Pane | None = None) -> None:
        """
        Queue `event` for each plugin that hears it. `pane` is the pane it
        happened in: one that mirrors another server's pane is left out,
        because the plugins of the server that owns it hear it there, and
        one event would wake two of them.
        """
        if pane is not None and isinstance(pane.process, RemoteProgram):
            return
        for slot in self.slots.values():
            if slot.state is PluginState.RUNNING and type(event) in slot.handles:
                try:
                    slot.offer(event)
                except Exception:
                    logger.exception("pymux: the plugin %s could not take %r", slot.name, event)

    async def keep(self, environ: Mapping[str, str] | None = None) -> None:
        "Run every plugin found, until the server ends."
        from pymux import plugins

        async with anyio.create_task_group() as tasks:
            for name, module in plugins.modules(environ):
                factory = getattr(module, "plugin", None)
                if factory is not None:
                    tasks.start_soon(self.serve, name, factory)

    async def serve(self, name: str, factory: Callable[[Host], Plugin]) -> None:
        """
        Run one plugin: its `run`, and its events one after another.

        **An exception stops here.** In the server's task group it would
        cancel the siblings, and the siblings are every client. The
        plugin is marked failed and says why in `show-plugins`.
        """
        send, receive = anyio.create_memory_object_stream[Event](QUEUE_LENGTH)
        slot = self.slots[name] = Slot(name, send, receive)
        try:
            async with anyio.create_task_group() as tasks:
                plugin = slot.plugin = factory(Host(self.pymux, name, tasks))
                slot.handles = handled(type(plugin))
                tasks.start_soon(plugin.run)
                tasks.start_soon(self._deliver, slot)
        except Exception as error:
            logger.exception("pymux: the plugin %s failed", name)
            cause = _first(error)
            slot.state = PluginState.FAILED
            slot.error = "%s: %s" % (type(cause).__name__, cause)
            self.pymux.show_message("The plugin %s failed: %s" % (name, slot.error))
        finally:
            if slot.state is PluginState.RUNNING:
                slot.state = PluginState.STOPPED
            send.close()
            receive.close()

    @staticmethod
    async def _deliver(slot: Slot) -> None:
        async for event in slot.receive:
            await getattr(slot.plugin, event.HANDLER)(event)

    def listing(self) -> list[str]:
        "One line per plugin: its name, its state, what it lost and why it failed."
        lines = []
        for name, slot in sorted(self.slots.items()):
            line = "%s: %s" % (name, slot.state)
            if slot.dropped:
                line += ", %d events dropped" % slot.dropped
            if slot.error is not None:
                line += ", %s" % slot.error
            lines.append(line)
        return lines


def _first(error: BaseException) -> BaseException:
    "The exception a task group wrapped, when it wrapped one."
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error
