"""
The class a plugin implements, and the host it gets.

A module in `pymux/plugins/` or `$XDG_CONFIG_HOME/pymux/plugins` names
its plugin as `plugin`: a callable that takes a `Host` and gives a
`Plugin`, which a subclass of `Plugin` already is. The server calls it
once, in `Pymux.running()`. The command line and the shell completion
import the same modules for their commands and never call it, so an
import that costs time belongs in `run`, not at the top of the module.

A plugin overrides the `on_` methods of the events it wants; the
others are never queued for it. It hears them one at a time, so a
handler that awaits a slow answer holds every event after it: long
work goes to `host.start_soon`. Everything runs on the server's loop,
so a call that blocks blocks every client.

A hot upgrade execs the new build from inside the server's scope: no
`finally` of a plugin runs, and the new process starts each plugin
again. What a plugin must keep, it writes down as it goes.
Lillecarl/pyterm#982.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from libpymux import CommandResult, Server

from ..log import logger
from ..mirror.plugin_messages import PeerMessage, PeerUnanswered, reachable

if TYPE_CHECKING:
    import anyio.abc

    from ..core.main import Pymux
    from ..core.pane import Pane
    from ..ids import PaneId
    from .events import (
        BellRang,
        CommandFinished,
        Notified,
        PaneEnded,
        ProgramReported,
        WindowActive,
        WindowSilent,
    )

__all__ = ["Host", "NoSocket", "Plugin"]


class NoSocket(RuntimeError):
    "The server listens on no socket, so a command has no way in."


class Host:
    """
    What one plugin may reach in the server that runs it.

    `pymux` is the whole server, for what the rest does not give. It is
    pymux's inside and changes with it.
    """

    def __init__(self, pymux: Pymux, name: str, tasks: anyio.abc.TaskGroup) -> None:
        self.pymux = pymux
        self.name = name
        self.log: logging.Logger = logger.getChild("plugin." + name)
        self._tasks = tasks

    def start_soon(self, func: Callable[..., Coroutine[Any, Any, Any]], *args: Any) -> None:
        "Run a task beside the plugin. An exception in it fails the plugin, and never the server."
        self._tasks.start_soon(func, *args)

    @property
    def server(self) -> Server:
        """
        This server, as a client sees it: what pymux-mcp drives, so its
        operations run here too.

        Its commands carry no environment: the server's own names
        whoever started it, maybe an agent, and a command would act in
        that agent's name.
        """
        if self.pymux.socket_name is None:
            raise NoSocket("this pymux server listens on no socket")
        return Server(self.pymux.socket_name, environment={})

    async def command(self, command: str, pane_id: PaneId | None = None) -> CommandResult:
        """
        Run one pymux command, as `pymux <command>` typed in the pane
        `pane_id`, and read its answer. A failed command answers, and
        does not raise.
        """
        return await self.server.cmd(command, pane_id=None if pane_id is None else "%%%d" % pane_id, check=False)

    def pane(self, pane_id: PaneId) -> Pane | None:
        return self.pymux.panes_by_id.get(pane_id)

    def peers(self) -> list[str]:
        """
        The linked servers `ask_peer` reaches, by name: each server that
        dialled in, by its host, the one a person used last first; then
        each address this server dialled.
        """
        return [name for name, _ in reachable(self.pymux)]

    async def ask_peer(self, peer: str, data: object, timeout: float) -> object:
        """
        Send `data` to the plugin of this plugin's name on the linked
        server `peer`, and wait up to `timeout` seconds for its answer.
        Both are JSON. Raises `PeerUnanswered` saying why there is none:
        no such link, no such plugin there, the plugin raised, the link
        went, or the time ran out. Lillecarl/pyterm#988.
        """
        found = [asks for name, asks in reachable(self.pymux) if name == peer]
        if not found:
            linked = self.peers()
            raise PeerUnanswered(
                "no linked server %s takes plugin messages%s"
                % (peer, "; linked: %s" % (", ".join(linked),) if linked else "")
            )
        if len(found) > 1:
            raise PeerUnanswered("%d linked servers answer to %s" % (len(found), peer))
        try:
            return await found[0].ask(self.name, data, timeout)
        except PeerUnanswered as why:
            raise PeerUnanswered("%s: %s" % (peer, why)) from None


class Plugin:
    "An event plugin. Every method is a no-op until a subclass gives it a body."

    def __init__(self, host: Host) -> None:
        self.host = host

    async def run(self) -> None:
        "The plugin's own work, beside its events. Returning ends that work and not the plugin."

    async def on_notified(self, event: Notified) -> None:
        pass

    async def on_program_reported(self, event: ProgramReported) -> None:
        pass

    async def on_command_finished(self, event: CommandFinished) -> None:
        pass

    async def on_bell(self, event: BellRang) -> None:
        pass

    async def on_pane_ended(self, event: PaneEnded) -> None:
        pass

    async def on_window_active(self, event: WindowActive) -> None:
        pass

    async def on_window_silent(self, event: WindowSilent) -> None:
        pass

    async def on_peer_message(self, message: PeerMessage) -> object:
        """
        The answer, as JSON, to a message from the plugin of the same name
        on a linked server (`Host.ask_peer`). Each message runs in a task
        of its own and holds no event. An exception goes back to the asker
        as the reason, and the plugin runs on. A plugin that leaves this
        out takes no messages.

        `message.peer` names the asker. Check it before acting: every
        server linked to this one can send a message.
        """
        raise NotImplementedError
