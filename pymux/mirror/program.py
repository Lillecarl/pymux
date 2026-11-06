"""
What runs behind a pane that mirrors a pane of another server.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from pyte.keep import Keep

from .sink import MirrorSink
from .window import RemoteWindow

if TYPE_CHECKING:
    import anyio.abc
    from ptterm.control import TerminalControl
    from pyte.keys.events import KeyProfile
    from pyte.mouse import MouseInput
    from pyte.sequences.kitty_dnd import DndPacket

    from ..core.pane import Pane
    from .copying import Place
    from .peer import Peer

__all__ = ["HOLD_FOR_FRESH", "RemoteProgram", "owner_pane_id"]

#: How long a client holds its frames for the first frame of a mirror it
#: shows again (`FrameHolds`). Without it the pane shows its last page
#: for a round trip, and then jumps to the new one.
HOLD_FOR_FRESH = 0.2


class _NoMachinery:
    "No process runs here, so there is nothing to ask about one."

    pid: int | None = None
    more_is_waiting = False


class RemoteProgram:
    """
    A pane of another server, where a pane expects a program: the owner
    runs it and this side shows its screen.

    It is the control's `Mirror`: keys, a paste and the mouse go to the
    owner as a person made them, and the owner encodes them for the
    modes the program set, which only its screen holds.

    **The page streams only while somebody here can see it.** The
    first sight asks the owner for it, and losing sight pauses the
    mirror; the owner sends what changed when it is seen again.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "peer": Keep.REBUILT,
        "pane_id": Keep.SAVED,
        "sink": Keep.REBUILT,
        "sx": Keep.SAVED,
        "sy": Keep.SAVED,
        "backend": Keep.REBUILT,
        "command": Keep.DROPPED,
        "visible": Keep.DROPPED,
        "suspended": Keep.DROPPED,
        "_stopped": Keep.DROPPED,
        "_asked": Keep.DROPPED,
        "_streaming": Keep.DROPPED,
        "stale": Keep.DROPPED,
    }

    @classmethod
    def of(cls, peer: Peer, pane_id: int) -> Callable[[TerminalControl], RemoteProgram]:
        "What builds a mirror of `pane_id` on `peer` for a pane's control (`PaneMaker.create_pane`)."
        return lambda control: cls(control, peer, pane_id)

    def __init__(self, control: TerminalControl, peer: Peer, pane_id: int) -> None:
        self.peer = peer
        self.pane_id = pane_id
        pymux = peer.tree.pymux
        self.sink = MirrorSink(control, pymux.terminals.sync_pointer_shape, self._fresh, pymux.drag_and_drop.sync_all)
        control.mirror = self
        self.sx = 0
        self.sy = 0
        self.backend = _NoMachinery()
        #: The program the owner says runs in the pane (`tree`).
        self.command: str | None = None
        #: A client here shows the pane (`Peers.sync_sight`).
        self.visible = False
        self.suspended = False
        self._stopped = False
        # The owner was asked for the screen once, and streams it now.
        self._asked = False
        self._streaming = False
        #: The page here is older than the owner's: it was asked for
        #: again, and the frame that catches it up has not come.
        self.stale = False

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        "Nothing starts here: the owner runs the program."

    def get_name(self) -> str:
        return self.command or "%s %%%d" % (self.peer.address, self.pane_id)

    def seen(self, visible: bool) -> bool:
        "Whether a client here shows the pane now. True when its page went stale for it: hold the frames."
        self.visible = visible
        return self._stream()

    def _stream(self) -> bool:
        "Ask the owner for frames while the pane is seen, and stop them while it is not. True when it asked again."
        wanted = self.visible and not self.suspended and not self._stopped
        if wanted == self._streaming:
            return False
        self._streaming = wanted
        if wanted and self.peer.connected:
            self.stale = True
        if wanted and not self._asked:
            self._asked = True
            self.peer.mirror(self.pane_id, self.sink)
        elif wanted:
            self.peer.resume(self.pane_id)
        else:
            self.peer.pause(self.pane_id)
        return wanted and self.stale

    def _fresh(self) -> None:
        self.stale = False

    def get_cwd(self) -> None:
        return None

    def set_size(self, width: int, height: int) -> None:
        # The owner sizes the pane, and its size arrives with the frames.
        self.sx = width
        self.sy = height

    def write_input(self, data: str) -> None:
        """
        Bytes for the program, such as a terminal's reply: the owner
        writes them as they are. A person's keys go through `key`, which
        the owner encodes.
        """
        if not self._stopped:
            self.peer.write(self.pane_id, data)

    def key(self, data: str, profile: KeyProfile) -> None:
        if not self._stopped:
            self.peer.key(self.pane_id, data, profile)

    def paste(self, text: str) -> None:
        if not self._stopped:
            self.peer.paste(self.pane_id, text)

    def mouse(self, mouse: MouseInput) -> None:
        if not self._stopped:
            self.peer.mouse(self.pane_id, mouse)

    def dnd(self, packet: DndPacket) -> None:
        "A drag and drop packet for the program, from a terminal here (`DragAndDrop._tell`)."
        if not self._stopped:
            self.peer.dnd(self.pane_id, packet.param())

    def kitty_transfer(self, param: str) -> None:
        "An OSC 5113 command for the program, from a terminal here (`KittyTransfers._tell`)."
        if not self._stopped:
            self.peer.kitty_transfer(self.pane_id, param)

    def kitty_transfer_taken(self, session_id: str) -> None:
        "Ask the owner when the program took what session `session_id` sent it (`KittyTransfers.taken`)."
        if not self._stopped:
            self.peer.kitty_transfer_taken(self.pane_id, session_id)

    def copy_mode(self, at: Place | None = None) -> None:
        "Copy mode runs on the owner, which holds the history (`Mirror.copy_mode`)."
        if not self._stopped:
            self.peer.copy_mode(self.pane_id, at)

    @property
    def copying(self) -> Place | None:
        return self.sink.copying

    def focus(self, focused: bool) -> None:
        "A terminal window with the focus shows this pane, or stopped (`Focus.sync_panes`)."
        if not self._stopped:
            self.peer.focus(self.pane_id, focused)

    def suspend(self) -> None:
        self.suspended = True
        self._stream()

    def resume(self) -> None:
        self.suspended = False
        self._stream()

    def kill(self) -> None:
        "Stop mirroring. The pane runs on, on its own server."
        if not self._stopped:
            self._stopped = True
            if self._asked:
                self.peer.stop(self.pane_id)

    def hang_up(self) -> None:
        self.kill()

    @property
    def is_terminated(self) -> bool:
        return self._stopped or self.sink.refused is not None


def owner_pane_id(pane: Pane) -> int | None:
    "The id of the pane on the server that owns it, for a pane mirrored here: a terminal or a window."
    if isinstance(pane.process, RemoteProgram):
        return pane.process.pane_id
    host = getattr(pane.terminal, "host", None)
    return host.pane_id if isinstance(host, RemoteWindow) else None
