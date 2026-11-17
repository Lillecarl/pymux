"""
A Wayland compositor the server holds, for the panes that have none.

It is optional twice over. `pysmithay` is an extra, Rust and Linux only,
and this module imports it or does nothing; `wayland-server` turns it
off where it loads. Lillecarl/pyterm#385.

What it serves today is the clipboard and the primary selection. A pane
whose session names no `WAYLAND_DISPLAY` is given this one, so wl-copy
and wl-paste work there. A session that has a display of its own -- a
desktop attached, an ssh client forwarding one -- keeps it: a program
started there belongs on that screen, not on one nobody looks at.

`set-clipboard` decides how far a selection reaches, as it does for OSC
52:

- Under "on" the person's terminal holds the truth. A pane's copy is
  written there with OSC 52 and the compositor takes the selection
  back at once; a paste asks the terminal of the client used last,
  and falls back to the paste buffer, or to the last primary a pane
  copied, when the terminal does not answer. Most terminals refuse
  clipboard reads unless told otherwise.
- Otherwise the panes share their selections among themselves and
  nothing more: a copy stays with the pane that made it, and a paste
  never reads what the person copied.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import errno
import logging
import math
import os
import signal
import socket
import sys
from collections.abc import Awaitable, Callable, Iterable
from subprocess import DEVNULL
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
from libpymux.protocol import Capability, Field, Packet
from prompt_toolkit.application.current import get_app
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.keys import Keys
from ptyhost.backends.posix import reaped_now
from ptyhost.spawn import Spawn, fork_on_pipes

from pymux.connection.clipboard_reads import CLIPBOARD_ASK_SECONDS, CLIPBOARD_READ_SECONDS
from pymux.enums import Clipboard, WindowScroll, Woke
from pymux.graphics.window_frames import FrameRects, VideoFrame, WindowView
from pymux.ids import PaneId
from pymux.jobs.pipes import JobPipes, wait_for
from pymux.mirror.program import RemoteProgram
from pymux.options.tables import CompositorRow, WindowPaneRow, WindowProgramRow
from pymux.wayland.wayland_scale import DEFAULT_WAYLAND_SCALE, resolve_scale
from pymux.wayland.window_pane import WindowWidget, evdev_of, now

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.containers import WritePosition
    from pysmithay import RemoteCompositor, RemoteEncoder

    from pymux.connection.client_state import ClientState
    from pymux.core.main import Pymux
    from pymux.core.pane import Pane

try:
    import pysmithay
    from pysmithay.host import command as host_command
except ImportError:
    pysmithay = None

logger = logging.getLogger(__name__)

#: The mime types that are text, best first. wl-copy offers the first
#: two for text it was given; X11 programs bridged in offer the rest.
TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING", "TEXT")

#: What the compositor offers when it holds a selection itself.
HELD_TYPES = TEXT_TYPES[:3]

#: What it offers besides while it holds an image from the person's
#: machine. Lillecarl/pyterm#958.
IMAGE_TYPE = "image/png"

#: How long a terminal's paste into a window answers the window's reads.
#: Its Ctrl+V reads at once; a later Ctrl+V asks the terminal again, which
#: may hold something newer by then.
PASTE_SECONDS = 2.0
#: OSC 52's letter for each selection.
OSC52_LETTER = {"clipboard": "c", "primary": "p"}

#: How long a client may take to hand its selection over, and how much
#: of it is read. A clipboard is a person's copy, not a file transfer.
READ_SECONDS = 5.0
READ_LIMIT = 16 << 20

#: How long a pasting client may take to read what it asked for.
WRITE_SECONDS = 30.0

#: How long to wait for a socket path another process holds, and how
#: often to try it. An upgrade's old server holds it for as long as the
#: new one takes to load, which `upgrade.LOAD_TIMEOUT` bounds.
BIND_SECONDS = 120.0

#: How long the compositor's process has to end once told to, before it
#: is killed: it closes every window first.
CHILD_EXIT_SECONDS = 5.0

#: How long after the compositor's process ended on its own a new one
#: starts: a child that dies at once must not spin the server.
RESTART_SECONDS = 1.0


class _Pacer:
    "The frames of one window that clients showed and the window was not yet told of."

    def __init__(self) -> None:
        self.shown = anyio.Event()
        #: Per client that showed one, a wait for its terminal to take it.
        self.drained: list[Callable[[], Awaitable[None]]] = []
        #: Whether pixels went to any client for the frames shown.
        self.sent = False
        #: Set by the window's next commit, and then replaced.
        self.committed = anyio.Event()

    def commit(self) -> None:
        self.committed.set()
        self.committed = anyio.Event()

    def shown_by(self, drained: Callable[[], Awaitable[None]], sent: bool) -> None:
        # Once per client: a client shows a window on every frame of its
        # own, and `_first` starts a task for each wait.
        if drained not in self.drained:
            self.drained.append(drained)
        self.sent = self.sent or sent
        self.shown.set()


#: How much of what a `pymux wl` program writes is kept, from the end.
#: It is shown when the program ends before it opens a window.
LAUNCH_TAIL = 8192

#: The refresh rate pysmithay's output advertises, in frames a second.
OUTPUT_REFRESH = 60


class _Inherited:
    """
    The compositor's process after an upgrade's exec: still this pid's
    child, and no `anyio` process holds it. Lillecarl/pyterm#1004.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            ended, code = reaped_now(self.pid)
            if not ended:
                code = await anyio.to_thread.run_sync(wait_for, self.pid, abandon_on_cancel=True)
            self.returncode = -1 if code is None else code
        return self.returncode

    def kill(self) -> None:
        if self.returncode is None:
            with contextlib.suppress(OSError):
                os.kill(self.pid, signal.SIGKILL)


@dataclasses.dataclass
class Inheritance:
    "What a snapshot hands the next image of the compositor, for `keep` to adopt."

    compositor: CompositorRow
    #: The window panes the snapshot built, by the compositor's id for each window.
    panes: dict[int, Pane]
    #: Each `pymux wl` program, and its pipes.
    programs: list[tuple[WindowProgramRow, JobPipes]]


class Launch:
    """
    A program `pymux wl` started, and the pane its first window takes.
    Its pid and pipes cross `upgrade-server`, as a job's do; an adopted
    one has no pane left to take.
    """

    def __init__(self, pane: Pane | None, pipes: JobPipes) -> None:
        self.pane = pane
        self.pipes = pipes
        #: The window pane that took `pane`'s place, once it did.
        self.adopted: Pane | None = None
        self.opened = anyio.Event()
        self.exited = anyio.Event()
        self.returncode: int | None = None
        self.tail = bytearray()

    def heard(self, data: bytes) -> None:
        self.tail += data
        del self.tail[:-LAUNCH_TAIL]


async def _first(waits: list[Callable[[], Awaitable[None]]]) -> None:
    "Wait for the first of these to finish; none waits for nothing."
    if not waits:
        return
    async with anyio.create_task_group() as group:

        async def one(wait: Callable[[], Awaitable[None]]) -> None:
            await wait()
            group.cancel_scope.cancel()

        for wait in list(waits):
            group.start_soon(one, wait)


def speaks(version: int) -> bool:
    "Whether this build can adopt a compositor that speaks protocol `version`."
    return pysmithay is not None and pysmithay.RemoteCompositor.speaks(version)


def available() -> bool:
    "Whether a compositor can run here at all."
    return pysmithay is not None and sys.platform == "linux"


def socket_path_of(server_socket: str | None) -> str | None:
    """
    Where the compositor listens: beside the server's own socket, so the
    path stays the same across `upgrade-server` and a pane keeps a
    working `WAYLAND_DISPLAY` through one. With no socket, the first
    free `wayland-N` in `$XDG_RUNTIME_DIR`.

    The name ends in ".sock" because Wayland's lock file replaces the
    extension, and a dotted server name would otherwise share one.
    """
    if not server_socket:
        return None
    directory, name = os.path.split(server_socket)
    return os.path.join(directory, "wayland-%s.sock" % (name,))


class WaylandServer:
    "The compositor of one pymux server, while `wayland-server` is on."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.compositor: RemoteCompositor | None = None
        #: The compositor's process, while it runs.
        self._process: anyio.abc.Process | _Inherited | None = None
        #: Stopped for `upgrade-server`.
        self.paused = False
        #: Paused for an upgrade's exec to carry, until `take_back`.
        self.handed_over = False
        #: What a snapshot loaded, until `keep` adopts it.
        self.inherited: Inheritance | None = None
        #: What `WAYLAND_DISPLAY` says to reach it, while it runs.
        self.display: str | None = None
        #: The text a pane last copied, per selection.
        self.copied: dict[str, str] = {}
        #: The image the person's machine held at their last Ctrl-V, which
        #: the clipboard offers as `IMAGE_TYPE`. Lillecarl/pyterm#958.
        self.image: bytes | None = None
        #: What the person's terminal last pasted into a window, which the
        #: clipboard offers to the reads its Ctrl+V starts.
        self.pasted: str | None = None
        self._pasted_until = 0.0
        #: Each open window, by the compositor's id, and the pane showing it.
        self.panes: dict[int, Pane] = {}
        #: The same windows' widgets, which the events update.
        self.widgets: dict[int, WindowWidget] = {}
        self._pacers: dict[int, _Pacer] = {}
        #: What each window was last configured with: width, height, scale.
        self._configured: dict[int, tuple[int, int, float]] = {}
        #: The cell each window was last sized for, which the pointer is read at.
        self._cells: dict[int, tuple[int, int]] = {}
        #: The programs `pymux wl` started, by their connection's tag.
        self._launches: dict[int, Launch] = {}
        self._tasks: anyio.abc.TaskGroup | None = None
        self._changed = anyio.Event()
        self._scope: anyio.CancelScope | None = None

    def option_changed(self) -> None:
        "`wayland-server` moved: start or stop on the next turn."
        self._changed.set()
        if not self.pymux.options.wayland_server and self._scope is not None:
            self._scope.cancel()

    async def keep(self) -> None:
        """
        Run the compositor whenever the option is on, and again after its
        process ends on its own: the panes keep a `WAYLAND_DISPLAY` that
        leads somewhere. Never returns.
        """
        while True:
            ended = False
            if (inherited := self.inherited) is not None:
                with anyio.CancelScope() as self._scope:
                    try:
                        ended = await self._serve_inherited(inherited)
                    except Exception:
                        logger.exception("The adopted Wayland compositor stopped.")
                        ended = True
                    finally:
                        self.inherited = None
                self._scope = None
            elif self.pymux.options.wayland_server and not self.paused and available():
                with anyio.CancelScope() as self._scope:
                    # Whatever a dead compositor's last calls raise ends
                    # it and its windows, and never the server.
                    try:
                        ended = await self._serve()
                    except Exception:
                        logger.exception("The Wayland compositor stopped.")
                        ended = True
                self._scope = None
            if ended and self.pymux.options.wayland_server and not self.paused:
                await anyio.sleep(RESTART_SECONDS)
                continue
            await self._changed.wait()
            self._changed = anyio.Event()

    def pids(self) -> list[int]:
        "The compositor's process, while it runs: pymux's own, for `renice`."
        process = self._process
        return [] if process is None or process.returncode is not None else [process.pid]

    async def pause(self) -> None:
        """
        Stop the compositor's process for `upgrade-server`, and wait until
        it has gone: the exec keeps this pid, and the new image would
        never reap it. Its windows closed already.
        """
        self.paused = True
        process = self._process
        if self._scope is not None:
            self._scope.cancel()
        if process is not None:
            with anyio.move_on_after(2 * CHILD_EXIT_SECONDS):
                await process.wait()

    def resume(self) -> None:
        "An upgrade that failed: serve again."
        self.paused = False
        self._changed.set()

    def holds_windows(self) -> bool:
        "Whether a window or a `pymux wl` program would end with the compositor."
        return bool(self.panes or self._launches)

    def hand_over(self) -> None:
        """
        Stop the compositor's process writing, at a message boundary, for
        an upgrade's exec to carry it with its windows. No await: nothing
        reads it from here to the exec. Raises `OSError` when it does not
        answer.
        """
        if self.compositor is None:
            return
        # First: a pause that times out may still land, and `take_back`
        # then has to resume it.
        self.handed_over = True
        # Nothing read after the snapshot that names these pipes.
        for launch in self._launches.values():
            launch.pipes.pause_reading()
        self.compositor.pause()

    def take_back(self) -> None:
        "An upgrade that failed after `hand_over`: the same process writes again."
        compositor, tasks = self.compositor, self._tasks
        if not self.handed_over or compositor is None or tasks is None:
            return
        self.handed_over = False
        for launch in self._launches.values():
            launch.pipes.resume_reading()
        try:
            windows = compositor.resume()
        except OSError as error:
            # `dispatch` hears that it went.
            logger.warning("The Wayland compositor did not resume: %s", error)
            return
        self._reconcile(compositor, dict(self.panes), windows, tasks)

    def snapshot_rows(self) -> list[CompositorRow | WindowPaneRow | WindowProgramRow]:
        "What an upgrade's snapshot holds of the compositor, its windows and their programs."
        rows: list[CompositorRow | WindowPaneRow | WindowProgramRow] = []
        compositor, process = self.compositor, self._process
        if compositor is None or process is None:
            return rows
        rows.append(CompositorRow(1, compositor.fileno(), process.pid, compositor.version))
        rows.extend(WindowPaneRow(pane.pane_id, toplevel) for toplevel, pane in self.panes.items())
        for tag, launch in self._launches.items():
            pipes = launch.pipes
            if not pipes.reaped:
                adopted = None if launch.adopted is None else launch.adopted.pane_id
                rows.append(WindowProgramRow(tag, pipes.pid, pipes.stdout, pipes.stderr, adopted))
        return rows

    def carried(self) -> list[int]:
        "The fds an upgrade's exec carries for the compositor and its programs, as `snapshot_rows` names them."
        fds: list[int] = []
        for row in self.snapshot_rows():
            if isinstance(row, CompositorRow):
                fds.append(row.fd)
            elif isinstance(row, WindowProgramRow):
                fds += [row.stdout_fd, row.stderr_fd]
        inherited = self.inherited
        if inherited is not None:
            fds.append(inherited.compositor.fd)
            fds += [fd for _row, pipes in inherited.programs for fd in pipes.fds]
        return fds

    async def _serve(self) -> bool:
        "Whether the compositor's process ended on its own."
        if pysmithay is None:
            return False
        path = socket_path_of(self.pymux.socket_name)
        async with self._child(path) as compositor:
            if compositor is None:
                return False
            await self._serve_on(compositor, path)
            return True

    async def _serve_inherited(self, inherited: Inheritance) -> bool:
        """
        Adopt the compositor an upgrade's exec carried, with its windows.
        One that cannot be adopted is killed, and its windows end: `keep`
        starts a new one.
        """
        assert pysmithay is not None
        row = inherited.compositor
        process = _Inherited(row.pid)
        try:
            compositor, windows = await pysmithay.RemoteCompositor.adopt(row.fd, row.version)
        except OSError as error:
            logger.warning("The Wayland compositor could not be adopted: %s", error)
            with contextlib.suppress(OSError):
                os.close(row.fd)
            for _row, pipes in inherited.programs:
                pipes.send_signal(signal.SIGKILL)
            for pane in inherited.panes.values():
                self.pymux.panes.window_pane_ended(pane)
            process.kill()
            with anyio.CancelScope(shield=True):
                await process.wait()
            return True
        async with self._owning(process):
            await self._serve_on(compositor, socket_path_of(self.pymux.socket_name), inherited, windows)
        return True

    @contextlib.asynccontextmanager
    async def _owning(self, process: anyio.abc.Process | _Inherited) -> AsyncIterator[None]:
        "Hold the compositor's process; on the way out, let it end, or kill it."
        self._process = process
        try:
            yield
        finally:
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(CHILD_EXIT_SECONDS):
                    await process.wait()
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            self._process = None

    @contextlib.asynccontextmanager
    async def _child(self, path: str | None) -> AsyncIterator[RemoteCompositor | None]:
        """
        The compositor, in a process of its own (`pysmithay.host`), or None
        when there cannot be one: so that a window can outlive this
        server's image. Lillecarl/pyterm#1004.

        **A path in use is waited for**, by the child: a compositor that
        another process still holds lets go only when that process ends.
        Giving up there would leave this server with no compositor, and
        every pane with a `WAYLAND_DISPLAY` that leads nowhere.
        """
        assert pysmithay is not None
        ours, theirs = socket.socketpair()
        try:
            argv = host_command(theirs.fileno(), path, wait=BIND_SECONDS)
            process = await anyio.open_process(
                argv, pass_fds=(theirs.fileno(),), stdin=DEVNULL, stdout=DEVNULL, stderr=None
            )
        except OSError as error:
            ours.close()
            logger.warning("No Wayland compositor: %s", error)
            process = None
        finally:
            theirs.close()
        if process is None:
            yield None
            return
        async with self._owning(process):
            compositor = None
            try:
                try:
                    compositor = await pysmithay.RemoteCompositor.start(
                        ours.detach(), BIND_SECONDS + CHILD_EXIT_SECONDS
                    )
                except OSError as error:
                    # No XDG_RUNTIME_DIR, or a path another server keeps:
                    # the panes get no display, as without pysmithay.
                    logger.warning("No Wayland compositor: %s", error)
                yield compositor
            finally:
                if compositor is not None:
                    compositor.shutdown()

    async def _serve_on(
        self,
        compositor: RemoteCompositor,
        path: str | None,
        inherited: Inheritance | None = None,
        windows: list[tuple[int, int, str, str]] | None = None,
    ) -> None:
        self.compositor = compositor
        self.display = compositor.socket_name
        logger.info("Wayland compositor on %s", self.display)
        try:
            for target in OSC52_LETTER:
                _hold(compositor, target)
            async with anyio.create_task_group() as tasks:
                self._tasks = tasks
                if inherited is not None and windows is not None:
                    self._adopt(compositor, inherited, windows, tasks)
                for fd in compositor.fds():
                    tasks.start_soon(self._dispatch_on, compositor, fd, tasks)
        finally:
            compositor.shutdown()
            self.handed_over = False
            self._tasks = None
            self.compositor = None
            self.display = None
            # A window does not outlive its compositor.
            for toplevel in list(self.panes):
                self._closed(toplevel)
            if path:
                for leftover in (path, os.path.splitext(path)[0] + ".lock"):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(leftover)

    async def _dispatch_on(self, compositor: RemoteCompositor, fd: int, tasks: anyio.abc.TaskGroup) -> None:
        while True:
            await anyio.wait_readable(fd)
            try:
                events = compositor.dispatch()
            except OSError as error:
                logger.warning("The Wayland compositor's process ended: %s", error)
                tasks.cancel_scope.cancel()
                return
            for event in events:
                # One event that fails to land must not end the
                # compositor, and every window with it.
                try:
                    self._handle(compositor, event, tasks)
                except Exception:
                    logger.exception("Handling the Wayland event %r failed.", event[:2])

    def _handle(self, compositor: RemoteCompositor, event: tuple[Any, ...], tasks: anyio.abc.TaskGroup) -> None:
        "One event of `dispatch`."
        match event:
            case ("selection", str(target), list(mime_types)) if mime_types:
                tasks.start_soon(self._take_selection, compositor, target, mime_types)
            case ("send", str(target), str(mime_type), int(pipe)):
                tasks.start_soon(self._answer_paste, target, mime_type, pipe)
            case ("toplevel", int(toplevel), int(tag)):
                self._opened(compositor, toplevel, self._launches.get(tag), tasks)
            case ("title", int(toplevel), str(title)) if toplevel in self.widgets:
                self.widgets[toplevel].screen.titles.window = title
                self.pymux.invalidate(Woke.WINDOW_PANE_CHANGED)
            case ("app_id", int(toplevel), str(app_id)) if toplevel in self.widgets:
                self.widgets[toplevel].app_id = app_id
                self.pymux.invalidate(Woke.WINDOW_PANE_CHANGED)
            case ("pointer", int(toplevel), str(shape)) if toplevel in self.widgets:
                self.widgets[toplevel].screen.pointer_shapes.shape = shape
                self.pymux.terminals.sync_pointer_shape()
                # The shape goes out behind a frame (`forward_osc`).
                self.pymux.invalidate(Woke.WINDOW_PANE_CHANGED)
            case ("commit", int(toplevel), _damage) if toplevel in self.panes:
                if (pacer := self._pacers.get(toplevel)) is not None:
                    pacer.commit()
                # Straight to the clients, with no frame of the cells:
                # a redraw of every client for every window frame is
                # what a window drawing at the link's pace would cost.
                self.pymux.panes.window_pane_drew(self.panes[toplevel])
            case ("closed", int(toplevel)):
                self._closed(toplevel)

    # ------------------------------------------------------------------
    # Windows in panes. Lillecarl/pyterm#622.

    def _opened(
        self, compositor: RemoteCompositor, toplevel: int, launch: Launch | None, tasks: anyio.abc.TaskGroup
    ) -> None:
        widget = WindowWidget(self, toplevel, self.pymux)
        pane = None
        if launch is not None and launch.pane is not None and launch.adopted is None:
            pane = launch.adopted = self.pymux.panes.adopt_window_pane(launch.pane, widget)
            launch.opened.set()
        if pane is None:
            # A later window of a `pymux wl` program goes beside its first.
            pane = self.pymux.panes.open_window_pane(widget, beside=launch.adopted if launch is not None else None)
        if pane is None:
            # Nowhere to show it: no session has a window.
            self.close(toplevel)
            return
        self._shown(compositor, toplevel, pane, widget, tasks)

    def _shown(
        self, compositor: RemoteCompositor, toplevel: int, pane: Pane, widget: WindowWidget, tasks: anyio.abc.TaskGroup
    ) -> None:
        self.panes[toplevel] = pane
        self.widgets[toplevel] = widget
        self._pacers[toplevel] = pacer = _Pacer()
        tasks.start_soon(self._pace, compositor, toplevel, pacer)

    def _adopt(
        self,
        compositor: RemoteCompositor,
        inherited: Inheritance,
        windows: list[tuple[int, int, str, str]],
        tasks: anyio.abc.TaskGroup,
    ) -> None:
        "Take over the windows and programs an upgrade's exec carried."
        for row, pipes in inherited.programs:
            launch = Launch(None, pipes)
            launch.adopted = None if row.pane_id is None else self.pymux.panes_by_id.get(PaneId(row.pane_id))
            launch.opened.set()
            self._launches[row.tag] = launch
            tasks.start_soon(self._run, launch, row.tag)
        self._reconcile(compositor, inherited.panes, windows, tasks)
        # Until here an upgrade is refused: these panes were in no table.
        self.inherited = None
        logger.info("Adopted the Wayland compositor with %d windows.", len(windows))

    def _reconcile(
        self,
        compositor: RemoteCompositor,
        panes: dict[int, Pane],
        windows: list[tuple[int, int, str, str]],
        tasks: anyio.abc.TaskGroup,
    ) -> None:
        """
        Make the window panes match `windows`, what the compositor says is
        open after a pause: `panes` were shown before it. Each window is
        told its frame is done, because the commit it waits on was held.
        """
        open_now = set()
        for toplevel, tag, title, app_id in windows:
            open_now.add(toplevel)
            pane = panes.get(toplevel)
            if pane is None:
                self._opened(compositor, toplevel, self._launches.get(tag), tasks)
            elif toplevel not in self.panes:
                assert isinstance(pane.terminal, WindowWidget)
                self._shown(compositor, toplevel, pane, pane.terminal, tasks)
            if (widget := self.widgets.get(toplevel)) is not None:
                widget.screen.titles.window = title
                widget.app_id = app_id
            with contextlib.suppress(KeyError):
                compositor.frame_done(toplevel, now())
        for toplevel, pane in panes.items():
            if toplevel in open_now:
                continue
            if toplevel in self.panes:
                self._closed(toplevel)
            else:
                assert isinstance(pane.terminal, WindowWidget)
                pane.terminal.process.is_terminated = True
                self.pymux.panes.window_pane_ended(pane)
        self.pymux.invalidate(Woke.WINDOW_PANE_CHANGED)

    def _closed(self, toplevel: int) -> None:
        pane = self.panes.pop(toplevel, None)
        widget = self.widgets.pop(toplevel, None)
        pacer = self._pacers.pop(toplevel, None)
        self._configured.pop(toplevel, None)
        self._cells.pop(toplevel, None)
        if pacer is not None:
            pacer.shown.set()
        if pane is not None and widget is not None:
            widget.process.is_terminated = True
            self.pymux.panes.window_pane_ended(pane)

    async def _pace(self, compositor: RemoteCompositor, toplevel: int, pacer: _Pacer) -> None:
        """
        Let one window draw its next frame once a client has shown the
        last, no sooner than the frame rate of the pymux window holding
        it, and once that client's terminal has taken the bytes.

        A window no client shows -- in another pymux window, under a
        popup, or on terminals without kitty graphics -- is never let go,
        and waits as a hidden Wayland window does. Lillecarl/pyterm#631.
        """
        released = -math.inf
        while True:
            await pacer.shown.wait()
            if self._pacers.get(toplevel) is not pacer:
                return
            rate = self._rate_of(toplevel)
            if not pacer.sent:
                # No link paces a frame that sent nothing: the refresh
                # the output says it has does, or a window that commits
                # nothing in a loop spins the server.
                rate = min(rate, OUTPUT_REFRESH) if rate else OUTPUT_REFRESH
            if rate:
                await anyio.sleep_until(released + 1 / rate)
            await _first(pacer.drained)
            pacer.drained.clear()
            pacer.sent = False
            pacer.shown = anyio.Event()
            released = anyio.current_time()
            with contextlib.suppress(KeyError):
                compositor.frame_done(toplevel, now())

    async def launch(self, pane: Pane, argv: list[str], cwd: str | None, env: dict[str, str]) -> Launch:
        """
        Start a program whose first window takes `pane`'s place.

        It runs under the compositor and not under the caller: taking
        the pane kills the shell `pymux wl` ran in, and the command with
        it. Its first connection is handed over as `WAYLAND_SOCKET`, a
        socket pair with no path, so the window says which pane it is
        for and nothing is left behind. `WAYLAND_DISPLAY` names the
        shared socket for any later connection, whose windows go beside.
        `DISPLAY` is dropped, so a program that would open on an X
        server fails here, where `pymux wl` shows why.
        Raises OSError when the program cannot start. Lillecarl/pyterm#632.

        `XDG_SESSION_TYPE` says Wayland, which is what Chromium and Qt
        pick their platform by: Chromium went to X11 and found no
        display. The rest is software drawing, unless the caller chose:
        this compositor takes shared memory and offers no GPU buffers,
        and no Wayland message says so. Mesa's EGL then draws with
        llvmpipe into shared memory, measured silent with qutebrowser,
        where it otherwise hunts for a GPU first. Qt Quick draws without
        OpenGL, for a machine with no Mesa at all: there qutebrowser
        found no EGL and drew black.

        Under `wayland-portal private` the session bus is the portal's own,
        where it answers light or dark. Lillecarl/pyterm#814.
        """
        compositor, tasks = self.compositor, self._tasks
        if compositor is None or tasks is None or self.display is None:
            raise OSError("no Wayland compositor runs")
        tag, fd = compositor.connect()
        env = {key: value for key, value in env.items() if key not in ("DISPLAY", "WAYLAND_SOCKET")}
        env.update(WAYLAND_SOCKET=str(fd), WAYLAND_DISPLAY=self.display, XDG_SESSION_TYPE="wayland")
        if self.pymux.portal.private_address is not None:
            env["DBUS_SESSION_BUS_ADDRESS"] = self.pymux.portal.private_address
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        env.setdefault("QT_QUICK_BACKEND", "software")
        try:
            pid, stdout, stderr = fork_on_pipes(Spawn(list(argv), env, cwd, passed=(fd,)))
        finally:
            os.close(fd)
        launch = Launch(pane, JobPipes(pid, stdout, stderr, parent=True))
        self._launches[tag] = launch
        tasks.start_soon(self._run, launch, tag)
        return launch

    async def _run(self, launch: Launch, tag: int) -> None:
        pipes = launch.pipes
        try:
            async with anyio.create_task_group() as pumps:
                # Read to the end, or a program that writes a lot blocks.
                pumps.start_soon(pipes.pump, pipes.stdout, launch.heard)
                pumps.start_soon(pipes.pump, pipes.stderr, launch.heard)
                launch.returncode = await pipes.reap()
        except anyio.get_cancelled_exc_class():
            # Its compositor ended, and its windows with it.
            pipes.send_signal(signal.SIGKILL)
            raise
        except Exception:
            # Not into the compositor's task group, which would end every window.
            logger.exception("Reading the Wayland program %d failed.", pipes.pid)
        finally:
            self._launches.pop(tag, None)
            launch.exited.set()

    def _rate_of(self, toplevel: int) -> int:
        """
        The most frames a second this window may draw; 0 sets no limit.

        A pymux window's `frame-rate` caps it only where a person chose
        one, on that window or with `-g`. Otherwise the window goes at
        the pace its frames are taken, as a Wayland window on a real
        screen goes at the screen's: the default thirty is for the cells.
        Lillecarl/pyterm#638.
        """
        pane = self.panes.get(toplevel)
        found = None if pane is None else self.pymux.place_of_pane_id(pane.pane_id)
        if found is None:
            return 0
        session, window, _pane = found
        if window.frame_rate_set or "frame_rate" in session.arrangement.window_defaults:
            return window.options.frame_rate
        return 0

    def configure(self, toplevel: int, columns: int, rows: int) -> None:
        """
        Ask the window for the pixels of that many cells, at its scale.

        A pane asks on every draw, and only a change reaches the window:
        so a cell size a client answers late, or a new `wayland-scale`,
        resizes it at the next frame.
        """
        if self.compositor is None:
            return
        width, height = self._cell_of(toplevel)
        wanted = (columns * width, rows * height, self._scale_of(toplevel, height))
        self._cells[toplevel] = (width, height)
        if self._configured.get(toplevel) == wanted:
            return
        self._configured[toplevel] = wanted
        with contextlib.suppress(KeyError):
            self.compositor.configure(toplevel, *wanted)

    def rescale(self) -> None:
        "Configure every window again, after a `wayland-scale` changed."
        for toplevel, widget in self.widgets.items():
            if widget.columns and widget.rows:
                self.configure(toplevel, widget.columns, widget.rows)

    def _cell_of(self, toplevel: int) -> tuple[int, int]:
        "The cell of the client a person used last of those watching the session that holds this window."
        pane = self.panes.get(toplevel)
        found = None if pane is None else self.pymux.place_of_pane_id(pane.pane_id)
        return self.pymux.terminals.cell_size(None if found is None else found[0])

    def _scale_of(self, toplevel: int, cell_height: int) -> float:
        "The scale the `wayland-scale` of the window holding this one names."
        pane = self.panes.get(toplevel)
        found = None if pane is None else self.pymux.place_of_pane_id(pane.pane_id)
        value = DEFAULT_WAYLAND_SCALE if found is None else found[1].options.wayland_scale
        return resolve_scale(value, cell_height)

    def close(self, toplevel: int) -> None:
        if self.compositor is not None:
            with contextlib.suppress(KeyError):
                self.compositor.close(toplevel)

    def press(self, toplevel: int, modifiers: tuple[int, ...], code: int, time: int) -> None:
        "One key, pressed and let go, inside its modifiers."
        compositor = self.compositor
        if compositor is None:
            return
        with contextlib.suppress(KeyError):
            compositor.focus(toplevel)
            for modifier in modifiers:
                compositor.key(modifier, True, time)
            compositor.key(code, True, time)
            compositor.key(code, False, time)
            for modifier in reversed(modifiers):
                compositor.key(modifier, False, time)

    def paste(self, toplevel: int, text: str) -> None:
        """
        A terminal's paste into a window: the clipboard offers `text`, and
        the window gets Ctrl+V, the paste key of a GUI program. A terminal
        in the window pastes on Ctrl+Shift+V instead, and gets ^V.
        """
        compositor = self.compositor
        if compositor is None:
            return
        self.pasted = text
        self._pasted_until = anyio.current_time() + PASTE_SECONDS
        _hold(compositor, "clipboard", self.image is not None)
        found = evdev_of(Keys.ControlV)
        if found is not None:
            self.press(toplevel, *found, now())

    def type_text(self, toplevel: int, text: str) -> bool:
        """
        Text no key of the keymap types, given to the window as text:
        é, å, an emoji. False when the window has no text field enabled
        where its focus is, and then the text goes nowhere, as before.
        Lillecarl/pyterm#626.
        """
        compositor = self.compositor
        if compositor is None:
            return False
        try:
            compositor.focus(toplevel)
        except KeyError:
            return False
        return compositor.commit_text(text)

    def _pixel_of(
        self, toplevel: int, position: Point, fraction: tuple[float, float] | None = None
    ) -> tuple[float, float]:
        """
        Where in the window's pixels the pointer is: the cell, and where in
        it the terminal said, for one that reports pixels; else the middle.
        Lillecarl/pyterm#650. At the cell the window was sized for.
        """
        width, height = self._cells.get(toplevel) or self._cell_of(toplevel)
        across, down = fraction or self._pointer_fraction()
        return (position.x + across) * width, (position.y + down) * height

    def _pointer_fraction(self) -> tuple[float, float]:
        "Where in its cell the mouse report being handled was, from the client it came from."
        client = self.pymux.client_of(get_app())
        if client is None:
            return (0.5, 0.5)
        return client.client.pointer_fraction

    def _scrolls_smooth(self) -> bool:
        "Whether the client the scroll came from says `window-scroll smooth`."
        client = self.pymux.client_of(get_app())
        return client is None or client.options.window_scroll == WindowScroll.SMOOTH

    def pointer(
        self,
        toplevel: int,
        position: Point,
        button: int | None,
        pressed: bool,
        time: int,
        fraction: tuple[float, float] | None = None,
    ) -> None:
        "The pointer at a cell, and where in it: `fraction`, or what the client being handled reported."
        compositor = self.compositor
        if compositor is None:
            return
        x, y = self._pixel_of(toplevel, position, fraction)
        with contextlib.suppress(KeyError):
            compositor.pointer_motion(toplevel, x, y, time)
            if button is not None:
                if pressed:
                    compositor.focus(toplevel)
                compositor.pointer_button(button, pressed, time)

    def pointer_leave(self, toplevel: int, time: int) -> None:
        "The pointer left the pane of this window: its surface gets a leave."
        compositor = self.compositor
        if compositor is None:
            return
        with contextlib.suppress(KeyError):
            compositor.pointer_leave(toplevel, time)

    def scroll(
        self, toplevel: int, position: Point, steps: int, time: int, fraction: tuple[float, float] | None = None
    ) -> None:
        compositor = self.compositor
        if compositor is None:
            return
        x, y = self._pixel_of(toplevel, position, fraction)
        with contextlib.suppress(KeyError):
            compositor.pointer_motion(toplevel, x, y, time)
            if self._scrolls_smooth():
                compositor.pointer_scroll_smooth(toplevel, steps * self._cell_of(toplevel)[1], time)
            else:
                compositor.pointer_scroll(steps, time)

    async def drawn_again(self, toplevels: Iterable[int], timeout: float) -> None:
        """
        Let each window draw what it waits to draw, and wait until none
        wants another frame, or `timeout` passes: what a capture does
        first. A window no client shows is never told it may draw, so its
        last frame can be one the program would long have replaced. A
        window that wants no frame has drawn everything already, and
        costs nothing here.
        """
        compositor = self.compositor
        if compositor is None:
            return

        async def drawn(toplevel: int, pacer: _Pacer) -> None:
            with contextlib.suppress(KeyError):
                while compositor.wants_frame(toplevel):
                    committed = pacer.committed
                    compositor.frame_done(toplevel, now())
                    await committed.wait()

        with anyio.move_on_after(timeout):
            async with anyio.create_task_group() as tasks:
                for toplevel in toplevels:
                    if (pacer := self._pacers.get(toplevel)) is not None:
                        tasks.start_soon(drawn, toplevel, pacer)

    def frame(self, toplevel: int) -> tuple[int, int, bytes] | None:
        "The last frame the window drew, as width, height and RGBA pixels."
        if self.compositor is None:
            return None
        try:
            return self.compositor.frame(toplevel)
        except KeyError:
            return None

    def view_of(
        self, pane_id: int, widget: WindowWidget, write_position: WritePosition, drained: Callable[[], Awaitable[None]]
    ) -> WindowView:
        """
        How one client brings this window pane up to date. `drained`
        waits until that client's terminal has taken what it was sent.
        """
        toplevel = widget.toplevel

        async def update(outer_id: int, since: int | None) -> tuple[int, str | None, bool]:
            if self.compositor is None:
                return 0, None, False
            try:
                encoded = self.compositor.kitty_update(toplevel, outer_id, since)
            except KeyError:
                return 0, None, False
            return await encoded

        async def rects(since: int | None) -> FrameRects | None:
            if self.compositor is None:
                return None
            try:
                read = self.compositor.frame_rects(toplevel, since)
            except KeyError:
                return None
            return await read

        async def video(encoder: RemoteEncoder) -> VideoFrame | None:
            if self.compositor is None:
                return None
            try:
                read = self.compositor.vp9_frame(toplevel, encoder)
            except KeyError:
                return None
            return await read

        def shown(sent: bool) -> None:
            pacer = self._pacers.get(toplevel)
            if pacer is not None:
                pacer.shown_by(drained, sent)

        found = self.pymux.place_of_pane_id(PaneId(pane_id))

        return WindowView(
            pane_id=pane_id,
            x=write_position.xpos,
            y=write_position.ypos,
            width=write_position.width,
            height=write_position.height,
            update=update,
            shown=shown,
            rects=rects,
            codec=str(found[1].options.window_codec) if found is not None else "",
            video=video,
            encoder=self.compositor.vp9_encoder if self.compositor is not None else None,
        )

    async def _take_selection(self, compositor: RemoteCompositor, target: str, mime_types: list[str]) -> None:
        "A pane copied: read it, and pass it on as `set-clipboard` allows."
        mime_type = next((one for one in TEXT_TYPES if one in mime_types), None)
        if mime_type is None:
            return
        try:
            fd = compositor.request_selection(target, mime_type)
        except ValueError:
            return  # Replaced or gone before it was asked for.
        data = await _read_all(fd)
        if data is None:
            return
        text = data.decode("utf-8", "replace")
        self.copied[target] = text
        if target == "clipboard":
            # A copy here is newer than the image a Ctrl-V brought, and than a paste.
            self.image = None
            self.pasted = None
        if self.pymux.options.set_clipboard is not Clipboard.ON or not text:
            return
        if target == "clipboard":
            self.pymux.clipboard.set_data(ClipboardData(text))
        self.pymux.outward.write_user_clipboard(text, OSC52_LETTER[target])
        # The terminal holds it now, and the next paste asks there.
        _hold(compositor, target)

    def take_client_image(self, client_state: ClientState, data: str) -> None:
        """
        The image a client sent ahead of its Ctrl-V, as base64 PNG, or ""
        for none. It goes to the compositor of the pane the key goes to:
        the owner's, through the peer link, for a mirrored pane, queued
        ahead of the key. Lillecarl/pyterm#958.
        """
        try:
            image = base64.b64decode(data, validate=True) or None
        except ValueError:
            logger.warning("Dropped a clipboard image that is not base64.")
            return
        session = client_state.session
        window = session.arrangement.get_active_window_for(client_state.app) if session is not None else None
        program = window.active_pane.process if window is not None and window.active_pane is not None else None
        if isinstance(program, RemoteProgram):
            if program.peer.can(Capability.IMAGE_PASTE):
                program.peer.send({Field.CMD: Packet.MIRROR_CLIPBOARD_IMAGE, Field.DATA: data})
            return
        self.hold_image(image)

    def hold_image(self, image: bytes | None) -> None:
        """
        Offer `image` on the clipboard as `IMAGE_TYPE`, beside the text,
        or stop offering one. A client sends it ahead of each Ctrl-V, so
        a program that pastes an image on that key -- Claude Code asks
        `wl-paste -l` -- finds the person's. Lillecarl/pyterm#958.
        """
        logger.info("The clipboard offers an image of %d bytes.", len(image) if image is not None else 0)
        # Nothing to withdraw leaves the selection where it is: under
        # `set-clipboard off` a pane holds it, and its text stays.
        had, self.image = self.image is not None, image
        if self.compositor is not None and (image is not None or had):
            _hold(self.compositor, "clipboard", image is not None)

    async def _answer_paste(self, target: str, mime_type: str, fd: int) -> None:
        "A pane pasted what the compositor holds: find it, and write it."
        if mime_type == IMAGE_TYPE and target == "clipboard" and self.image is not None:
            await _write_all(fd, self.image)
            return
        if target == "clipboard" and self.pasted is not None and anyio.current_time() <= self._pasted_until:
            await _write_all(fd, self.pasted.encode("utf-8"))
            return
        if self.pymux.options.set_clipboard is Clipboard.ON:
            text = await self._read_the_terminal(target)
            if text is None:
                text = self._fallback(target)
        else:
            # Only a held image takes the selection here, from the pane
            # that copied this; the person's paste buffer stays theirs.
            text = self.copied.get(target, "")
        await _write_all(fd, text.encode("utf-8"))

    async def _read_the_terminal(self, target: str) -> str | None:
        """
        The selection of the terminal a person used last, or None: a
        client's here, or the person's at a peer, who is no client here.
        Lillecarl/pyterm#992.
        """
        client = self.pymux.latest_client()
        links = [link for link in self.pymux.peer_links() if link.can(Capability.CLIPBOARD_READS) and link.sources]
        link = max(links, key=lambda link: link.last_used, default=None)
        if link is not None and (client is None or link.last_used > client.last_used):
            # The peer waits on its terminal at most this long; the link adds its own trip.
            return await link.clipboard_asks.ask(OSC52_LETTER[target], CLIPBOARD_ASK_SECONDS + CLIPBOARD_READ_SECONDS)
        if client is None:
            return None
        return await client.client.clipboard.read(OSC52_LETTER[target])

    def _fallback(self, target: str) -> str:
        "What pymux itself knows was copied last."
        if target == "clipboard":
            return self.pymux.clipboard.get_data().text
        return self.copied.get(target, "")


def _hold(compositor: RemoteCompositor, target: str, image: bool = False) -> None:
    "Take the selection, answered by `_answer_paste` when a pane pastes."
    compositor.set_selection(target, dict.fromkeys((*HELD_TYPES, IMAGE_TYPE) if image else HELD_TYPES))


async def _read_all(fd: int) -> bytes | None:
    "A pipe read to its end, or None when the writer stalls or floods it."
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    size = 0
    try:
        with anyio.move_on_after(READ_SECONDS):
            while True:
                await anyio.wait_readable(fd)
                try:
                    chunk = os.read(fd, 1 << 16)
                except OSError as error:
                    if error.errno == errno.EAGAIN:
                        continue
                    raise
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > READ_LIMIT:
                    return None
                chunks.append(chunk)
        return None
    finally:
        os.close(fd)


async def _write_all(fd: int, data: bytes) -> None:
    "Write to a pasting client's pipe, and close it. A reader that left is fine."
    os.set_blocking(fd, False)
    view = memoryview(data)
    try:
        with anyio.move_on_after(WRITE_SECONDS):
            while view:
                await anyio.wait_writable(fd)
                try:
                    view = view[os.write(fd, view) :]
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    return
    finally:
        os.close(fd)
