"""
The hooks of the server: an event's name, and the commands it runs.
`set-hook` adds to them, and every wake that is an event fires one.
Lillecarl/pymux#297.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from pyte.keep import Keep

from .actions import Origin, started_by
from .enums import Woke

__all__ = ["Hooks"]

#: The hook a wake runs, when it has tmux's name for one. A command
#: that ran wakes as `Woke.COMMAND_RAN`, and every command has an
#: `after-` hook in tmux, so its name is read back off the reason.
_HOOKS_BY_WAKE: dict[str, str] = {
    Woke.PANE_WAS_SPLIT_OFF: "after-split-window",
    Woke.WINDOW_OPENED: "after-new-window",
    Woke.PANE_BROKE_OUT: "after-break-pane",
    Woke.PANE_WAS_RESPAWNED: "after-respawn-pane",
    Woke.PANE_ENDED: "pane-died",
    Woke.CLIENT_ATTACHED: "client-attached",
    Woke.CLIENT_DETACHED: "client-detached",
}

_COMMAND_RAN_BEFORE, _, _COMMAND_RAN_AFTER = Woke.COMMAND_RAN.partition("%s")


def hook_of(reason: str) -> str | None:
    "The hook name of one wake, or None for a wake that is not an event."
    if reason.startswith(_COMMAND_RAN_BEFORE) and reason.endswith(_COMMAND_RAN_AFTER):
        name = reason[len(_COMMAND_RAN_BEFORE) : len(reason) - len(_COMMAND_RAN_AFTER)]
        return "after-%s" % name

    return _HOOKS_BY_WAKE.get(reason)


class Hooks:
    "The commands of each hook, in the order they were given."

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    KEEP: ClassVar[dict[str, Keep]] = {
        "commands": Keep.SAVED,
        "_running": Keep.DROPPED,
    }

    def __init__(self) -> None:
        self.commands: dict[str, list[str]] = {}
        self._running: set[str] = set()

    def add(self, hook: str, command: str) -> None:
        self.commands.setdefault(hook, []).append(command)

    def forget(self, hook: str) -> None:
        self.commands.pop(hook, None)

    def fire(self, reason: str, run: Callable[[str], None]) -> None:
        """
        Run the commands of the hook this wake names, unless it is
        running already: a hook that runs a command fires the hooks of
        that command, and only the guard keeps that from walking in a
        circle.
        """
        name = hook_of(reason)
        if name is None or name in self._running:
            return
        commands = self.commands.get(name)
        if not commands:
            return
        self._running.add(name)
        try:
            with started_by(Origin.HOOK):
                for command in commands:
                    run(command)
        finally:
            self._running.discard(name)
