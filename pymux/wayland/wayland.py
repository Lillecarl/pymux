"""
A Wayland compositor the server holds, for the panes that have none.

It is optional twice over. `pysmithay` is an extra, Rust and Linux only,
and this module imports it or does nothing; `wayland-server` turns it
off where it loads. Lillecarl/pymux#383.

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

import contextlib
import errno
import logging
import math
import os
import sys
from collections.abc import Awaitable, Callable, Iterable
from subprocess import DEVNULL, PIPE, STDOUT
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
from prompt_toolkit.application.current import get_app
from prompt_toolkit.clipboard import ClipboardData

from pymux.enums import Clipboard, WindowScroll, Woke
from pymux.graphics.window_frames import FrameRects, VideoFrame, WindowView
from pymux.ids import PaneId
from pymux.niceness import as_a_person_runs
from pymux.wayland.wayland_scale import DEFAULT_WAYLAND_SCALE, resolve_scale
from pymux.wayland.window_pane import WindowWidget, now

if TYPE_CHECKING:
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.containers import WritePosition
    from pysmithay import Compositor, Vp9Encoder

    from pymux.core.main import Pymux
    from pymux.core.window_tree import Pane

try:
    import pysmithay
except ImportError:
    pysmithay = None

logger = logging.getLogger(__name__)

#: The mime types that are text, best first. wl-copy offers the first
#: two for text it was given; X11 programs bridged in offer the rest.
TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING", "TEXT")

#: What the compositor offers when it holds a selection itself.
HELD_TYPES = TEXT_TYPES[:3]

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
BIND_RETRY_SECONDS = 0.25


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


class Launch:
    "A program `pymux wl` started, and the pane its first window takes."

    def __init__(self, pane: Pane) -> None:
        self.pane = pane
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
        self.compositor: Compositor | None = None
        #: What `WAYLAND_DISPLAY` says to reach it, while it runs.
        self.display: str | None = None
        #: The text a pane last copied, per selection.
        self.copied: dict[str, str] = {}
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
        "Run the compositor whenever the option is on. Never returns."
        while True:
            if self.pymux.options.wayland_server and available():
                with anyio.CancelScope() as self._scope:
                    await self._serve()
                self._scope = None
            await self._changed.wait()
            self._changed = anyio.Event()

    async def _serve(self) -> None:
        if pysmithay is None:
            return
        path = socket_path_of(self.pymux.socket_name)
        compositor = await _bind(path)
        if compositor is None:
            return

        self.compositor = compositor
        self.display = compositor.socket_name
        logger.info("Wayland compositor on %s", self.display)
        try:
            for target in OSC52_LETTER:
                _hold(compositor, target)
            async with anyio.create_task_group() as tasks:
                self._tasks = tasks
                for fd in compositor.fds():
                    tasks.start_soon(self._dispatch_on, compositor, fd, tasks)
        finally:
            # Here, on the loop thread: the last reference may go on any.
            compositor.shutdown()
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

    async def _dispatch_on(self, compositor: Compositor, fd: int, tasks: anyio.abc.TaskGroup) -> None:
        while True:
            await anyio.wait_readable(fd)
            for event in compositor.dispatch():
                # One event that fails to land must not end the
                # compositor, and every window with it.
                try:
                    self._handle(compositor, event, tasks)
                except Exception:
                    logger.exception("Handling the Wayland event %r failed.", event[:2])

    def _handle(self, compositor: Compositor, event: tuple[Any, ...], tasks: anyio.abc.TaskGroup) -> None:
        "One event of `dispatch`."
        match event:
            case ("selection", str(target), list(mime_types)) if mime_types:
                tasks.start_soon(self._take_selection, compositor, target, mime_types)
            case ("send", str(target), str(_mime_type), int(pipe)):
                tasks.start_soon(self._answer_paste, target, pipe)
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
    # Windows in panes. Lillecarl/pymux#620.

    def _opened(self, compositor: Compositor, toplevel: int, launch: Launch | None, tasks: anyio.abc.TaskGroup) -> None:
        widget = WindowWidget(self, toplevel, self._takes_keys)
        pane = None
        if launch is not None and launch.adopted is None:
            pane = launch.adopted = self.pymux.panes.adopt_window_pane(launch.pane, widget)
            launch.opened.set()
        if pane is None:
            # A later window of a `pymux wl` program goes beside its first.
            pane = self.pymux.panes.open_window_pane(widget, beside=launch.adopted if launch is not None else None)
        if pane is None:
            # Nowhere to show it: no session has a window.
            self.close(toplevel)
            return
        self.panes[toplevel] = pane
        self.widgets[toplevel] = widget
        self._pacers[toplevel] = pacer = _Pacer()
        tasks.start_soon(self._pace, compositor, toplevel, pacer)

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

    async def _pace(self, compositor: Compositor, toplevel: int, pacer: _Pacer) -> None:
        """
        Let one window draw its next frame once a client has shown the
        last, no sooner than the frame rate of the pymux window holding
        it, and once that client's terminal has taken the bytes.

        A window no client shows -- in another pymux window, under a
        popup, or on terminals without kitty graphics -- is never let go,
        and waits as a hidden Wayland window does. Lillecarl/pymux#629.
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
        Raises OSError when the program cannot start. Lillecarl/pymux#630.

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
        where it answers light or dark. Lillecarl/pymux#812.
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
        launch = Launch(pane)
        self._launches[tag] = launch
        try:
            await tasks.start(self._run, launch, tag, argv, cwd, env, fd)
        finally:
            os.close(fd)
        return launch

    async def _run(
        self,
        launch: Launch,
        tag: int,
        argv: list[str],
        cwd: str | None,
        env: dict[str, str],
        fd: int,
        *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        try:
            async with await anyio.open_process(
                as_a_person_runs(argv),
                cwd=cwd,
                env=env,
                pass_fds=(fd,),
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=STDOUT,
                start_new_session=True,
            ) as process:
                task_status.started()
                assert process.stdout is not None
                # Read to the end, or a program that writes a lot blocks.
                async for data in process.stdout:
                    launch.heard(data)
                launch.returncode = await process.wait()
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
        Lillecarl/pymux#636.
        """
        pane = self.panes.get(toplevel)
        found = None if pane is None else self.pymux.window_of_pane(pane.pane_id)
        if found is None:
            return 0
        session, window, _pane = found
        if window.frame_rate_set or "frame_rate" in session.arrangement.window_defaults:
            return window.options.frame_rate
        return 0

    def _takes_keys(self) -> bool:
        "The person at the keyboard may drive it, and holds no prefix."
        try:
            state = self.pymux.get_client_state()
        except ValueError:
            return True
        return not state.read_only and not state.has_prefix

    def client_shows_windows(self) -> bool:
        "Whether the client drawing now can show a window: it speaks kitty."
        try:
            state = self.pymux.get_client_state()
        except ValueError:
            return False
        return bool(state.connection.graphics.kitty_supported)

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
        found = None if pane is None else self.pymux.window_of_pane(pane.pane_id)
        return self.pymux.terminals.cell_size(None if found is None else found[0])

    def _scale_of(self, toplevel: int, cell_height: int) -> float:
        "The scale the `wayland-scale` of the window holding this one names."
        pane = self.panes.get(toplevel)
        found = None if pane is None else self.pymux.window_of_pane(pane.pane_id)
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

    def type_text(self, toplevel: int, text: str) -> bool:
        """
        Text no key of the keymap types, given to the window as text:
        é, å, an emoji. False when the window has no text field enabled
        where its focus is, and then the text goes nowhere, as before.
        Lillecarl/pymux#624.
        """
        compositor = self.compositor
        if compositor is None:
            return False
        try:
            compositor.focus(toplevel)
        except KeyError:
            return False
        return compositor.commit_text(text)

    def _pixel_of(self, toplevel: int, position: Point) -> tuple[float, float]:
        """
        Where in the window's pixels the pointer is: the cell, and where in
        it the terminal said, for one that reports pixels; else the middle.
        Lillecarl/pymux#648. At the cell the window was sized for.
        """
        width, height = self._cells.get(toplevel) or self._cell_of(toplevel)
        across, down = self._pointer_fraction()
        return (position.x + across) * width, (position.y + down) * height

    def _pointer_fraction(self) -> tuple[float, float]:
        "Where in its cell the mouse report being handled was, from the client it came from."
        client = self.pymux.client_of(get_app())
        if client is None:
            return (0.5, 0.5)
        return client.connection.pointer_fraction

    def _scrolls_smooth(self) -> bool:
        "Whether the client the scroll came from says `window-scroll smooth`."
        client = self.pymux.client_of(get_app())
        return client is None or client.options.window_scroll == WindowScroll.SMOOTH

    def pointer(self, toplevel: int, position: Point, button: int | None, pressed: bool, time: int) -> None:
        compositor = self.compositor
        if compositor is None:
            return
        x, y = self._pixel_of(toplevel, position)
        with contextlib.suppress(KeyError):
            compositor.pointer_motion(toplevel, x, y, time)
            if button is not None:
                if pressed:
                    compositor.focus(toplevel)
                compositor.pointer_button(button, pressed, time)

    def scroll(self, toplevel: int, position: Point, steps: int, time: int) -> None:
        compositor = self.compositor
        if compositor is None:
            return
        x, y = self._pixel_of(toplevel, position)
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

        async def video(encoder: Vp9Encoder) -> VideoFrame | None:
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

        found = self.pymux.window_of_pane(PaneId(pane_id))

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
            encoder=None if pysmithay is None else pysmithay.Vp9Encoder,
        )

    async def _take_selection(self, compositor: Compositor, target: str, mime_types: list[str]) -> None:
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
        if self.pymux.options.set_clipboard is not Clipboard.ON or not text:
            return
        if target == "clipboard":
            self.pymux.clipboard.set_data(ClipboardData(text))
        self.pymux.outward.write_user_clipboard(text, OSC52_LETTER[target])
        # The terminal holds it now, and the next paste asks there.
        _hold(compositor, target)

    async def _answer_paste(self, target: str, fd: int) -> None:
        "A pane pasted what the compositor holds: find the text, and write it."
        text = ""
        if self.pymux.options.set_clipboard is Clipboard.ON:
            text = await self._read_the_terminal(target)
            if text is None:
                text = self._fallback(target)
        await _write_all(fd, text.encode("utf-8"))

    async def _read_the_terminal(self, target: str) -> str | None:
        "The selection of the terminal a person used last, or None."
        client = self.pymux.latest_client()
        if client is None:
            return None
        return await client.connection.clipboard.read(OSC52_LETTER[target])

    def _fallback(self, target: str) -> str:
        "What pymux itself knows was copied last."
        if target == "clipboard":
            return self.pymux.clipboard.get_data().text
        return self.copied.get(target, "")


async def _bind(path: str | None) -> Compositor | None:
    """
    A compositor on `path`, or None when there cannot be one.

    **A path in use is waited for.** `upgrade-server` starts the new
    build while the old one still holds the lock, and the old one lets
    go only when it exits, after the new one says it is ready. Giving up
    here left an upgraded server with no compositor, and every pane with
    a `WAYLAND_DISPLAY` that led nowhere.
    """
    assert pysmithay is not None
    deadline = anyio.current_time() + BIND_SECONDS
    while True:
        try:
            return pysmithay.Compositor(path)
        except OSError as error:
            if "in use" not in str(error) or anyio.current_time() > deadline:
                # No XDG_RUNTIME_DIR, or a path another server keeps:
                # the panes get no display, as without pysmithay.
                logger.warning("No Wayland compositor: %s", error)
                return None
        await anyio.sleep(BIND_RETRY_SECONDS)


def _hold(compositor: Compositor, target: str) -> None:
    "Take the selection, answered by `_answer_paste` when a pane pastes."
    compositor.set_selection(target, dict.fromkeys(HELD_TYPES))


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
