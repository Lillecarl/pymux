"""
What runs behind a pane that mirrors a pane of another server.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from pyte.keep import Keep

from .sink import MirrorSink

if TYPE_CHECKING:
    import anyio.abc
    from ptterm.control import TerminalControl

    from .peer import Peer

__all__ = ["RemoteProgram"]


class _NoMachinery:
    "No process runs here, so there is nothing to ask about one."

    pid: int | None = None


class RemoteProgram:
    """
    A pane of another server, where a pane expects a program: the owner
    runs it and this side shows its screen.

    Keys go to the owner as the bytes this pane encoded them to, for
    the keyboard mode the program asked the owner's screen for, which a
    mirror holds as well. Copy mode suspends it, which pauses the mirror
    so the history holds still; resuming sends what changed meanwhile.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "peer": Keep.REBUILT,
        "pane_id": Keep.SAVED,
        "sink": Keep.REBUILT,
        "sx": Keep.SAVED,
        "sy": Keep.SAVED,
        "backend": Keep.REBUILT,
        "suspended": Keep.DROPPED,
        "_stopped": Keep.DROPPED,
        "_started": Keep.DROPPED,
    }

    @classmethod
    def of(cls, peer: Peer, pane_id: int) -> Callable[[TerminalControl], RemoteProgram]:
        "What builds a mirror of `pane_id` on `peer` for a pane's control (`PaneMaker.create_pane`)."
        return lambda control: cls(control, peer, pane_id)

    def __init__(self, control: TerminalControl, peer: Peer, pane_id: int) -> None:
        self.peer = peer
        self.pane_id = pane_id
        self.sink = MirrorSink(control)
        self.sx = 0
        self.sy = 0
        self.backend = _NoMachinery()
        self.suspended = False
        self._stopped = False
        self._started = False

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        if not self._started:
            self._started = True
            self.peer.mirror(self.pane_id, self.sink)

    def get_name(self) -> str:
        return "%s %%%d" % (self.peer.address, self.pane_id)

    def get_cwd(self) -> None:
        return None

    def set_size(self, width: int, height: int) -> None:
        # The owner sizes the pane, and its resize arrives with the reads.
        self.sx = width
        self.sy = height

    def write_input(self, data: str) -> None:
        if not self._stopped:
            self.peer.type(self.pane_id, data)

    def focus(self, focused: bool) -> None:
        "A terminal window with the focus shows this pane, or stopped (`Focus.sync_panes`)."
        if not self._stopped:
            self.peer.focus(self.pane_id, focused)

    def suspend(self) -> None:
        if not self.suspended and not self._stopped:
            self.suspended = True
            self.peer.pause(self.pane_id)

    def resume(self) -> None:
        if self.suspended and not self._stopped:
            self.suspended = False
            self.peer.resume(self.pane_id)

    def kill(self) -> None:
        "Stop mirroring. The pane runs on, on its own server."
        if not self._stopped:
            self._stopped = True
            self.peer.stop(self.pane_id)

    def hang_up(self) -> None:
        self.kill()

    @property
    def is_terminated(self) -> bool:
        return self._stopped or self.sink.refused is not None
