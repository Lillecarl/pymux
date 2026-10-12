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
from collections.abc import Awaitable, Callable
from subprocess import DEVNULL, PIPE, STDOUT
from typing import TYPE_CHECKING

import anyio
import anyio.abc
from prompt_toolkit.clipboard import ClipboardData

from pymux.enums import Woke
from pymux.graphics import WindowView
from pymux.options import Clipboard
from pymux.window_pane import WindowWidget, now

if TYPE_CHECKING:
    from pysmithay import Compositor

    from pymux.arrangement import Pane
    from pymux.main import Pymux

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

    def shown_by(self, drained: Callable[[], Awaitable[None]]) -> None:
        self.drained.append(drained)
        self.shown.set()


#: How much of what a `pymux wl` program writes is kept, from the end.
#: It is shown when the program ends before it opens a window.
LAUNCH_TAIL = 8192


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
        #: The programs `pymux wl` started, by their connection's tag.
        self._launches: dict[int, Launch] = {}
        self._tasks: anyio.abc.TaskGroup | None = None
        self._changed = anyio.Event()
        self._scope: anyio.CancelScope | None = None

    def option_changed(self) -> None:
        "`wayland-server` moved: start or stop on the next turn."
        self._changed.set()
        if not self.pymux.wayland_server and self._scope is not None:
            self._scope.cancel()

    async def keep(self) -> None:
        "Run the compositor whenever the option is on. Never returns."
        while True:
            if self.pymux.wayland_server and available():
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

    def _handle(self, compositor: Compositor, event: tuple, tasks: anyio.abc.TaskGroup) -> None:
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
            case ("commit", int(toplevel), _damage) if toplevel in self.panes:
                # Straight to the clients, with no frame of the cells:
                # a redraw of every client for every window frame is
                # what a window drawing at the link's pace would cost.
                self.pymux.window_pane_drew(self.panes[toplevel])
            case ("closed", int(toplevel)):
                self._closed(toplevel)

    # ------------------------------------------------------------------
    # Windows in panes. Lillecarl/pymux#620.

    def _opened(self, compositor: Compositor, toplevel: int, launch: Launch | None, tasks: anyio.abc.TaskGroup) -> None:
        widget = WindowWidget(self, toplevel, self._takes_keys)
        pane = None
        if launch is not None and launch.adopted is None:
            pane = launch.adopted = self.pymux.adopt_window_pane(launch.pane, widget)
            launch.opened.set()
        if pane is None:
            # A later window of a `pymux wl` program goes beside its first.
            pane = self.pymux.open_window_pane(widget, beside=launch.adopted if launch is not None else None)
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
        if pacer is not None:
            pacer.shown.set()
        if pane is not None and widget is not None:
            widget.process.is_terminated = True
            self.pymux.window_pane_ended(pane)

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
            if rate:
                await anyio.sleep_until(released + 1 / rate)
            await _first(pacer.drained)
            pacer.drained.clear()
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
        """
        compositor, tasks = self.compositor, self._tasks
        if compositor is None or tasks is None or self.display is None:
            raise OSError("no Wayland compositor runs")
        tag, fd = compositor.connect()
        env = {key: value for key, value in env.items() if key not in ("DISPLAY", "WAYLAND_SOCKET")}
        env.update(WAYLAND_SOCKET=str(fd), WAYLAND_DISPLAY=self.display)
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
                argv,
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
            return window.frame_rate
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
        graphics = getattr(state.connection, "graphics", None)
        return bool(graphics is not None and graphics.kitty_supported)

    def configure(self, toplevel: int, columns: int, rows: int) -> None:
        "Ask the window for the pixels of that many cells."
        if self.compositor is None:
            return
        width, height = self.pymux.cell_size()
        with contextlib.suppress(KeyError):
            self.compositor.configure(toplevel, columns * width, rows * height)

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

    def _pixel_of(self, position) -> tuple[float, float]:
        "The middle of a cell, in the window's pixels."
        width, height = self.pymux.cell_size()
        return (position.x + 0.5) * width, (position.y + 0.5) * height

    def pointer(self, toplevel: int, position, button: int | None, pressed: bool, time: int) -> None:
        compositor = self.compositor
        if compositor is None:
            return
        x, y = self._pixel_of(position)
        with contextlib.suppress(KeyError):
            compositor.pointer_motion(toplevel, x, y, time)
            if button is not None:
                if pressed:
                    compositor.focus(toplevel)
                compositor.pointer_button(button, pressed, time)

    def scroll(self, toplevel: int, position, steps: int, time: int) -> None:
        compositor = self.compositor
        if compositor is None:
            return
        x, y = self._pixel_of(position)
        with contextlib.suppress(KeyError):
            compositor.pointer_motion(toplevel, x, y, time)
            compositor.pointer_scroll(steps, time)

    def view_of(
        self, pane_id: int, widget: WindowWidget, write_position, drained: Callable[[], Awaitable[None]]
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

        def shown() -> None:
            pacer = self._pacers.get(toplevel)
            if pacer is not None:
                pacer.shown_by(drained)

        return WindowView(
            pane_id=pane_id,
            x=write_position.xpos,
            y=write_position.ypos,
            width=write_position.width,
            height=write_position.height,
            update=update,
            shown=shown,
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
        if self.pymux.clipboard_mode is not Clipboard.ON or not text:
            return
        if target == "clipboard":
            self.pymux.clipboard.set_data(ClipboardData(text))
        self.pymux.write_user_clipboard(text, OSC52_LETTER[target])
        # The terminal holds it now, and the next paste asks there.
        _hold(compositor, target)

    async def _answer_paste(self, target: str, fd: int) -> None:
        "A pane pasted what the compositor holds: find the text, and write it."
        text = ""
        if self.pymux.clipboard_mode is Clipboard.ON:
            text = await self._read_the_terminal(target)
            if text is None:
                text = self._fallback(target)
        await _write_all(fd, text.encode("utf-8"))

    async def _read_the_terminal(self, target: str) -> str | None:
        "The selection of the terminal a person used last, or None."
        client = self.pymux.latest_client()
        connection = getattr(client, "connection", None)
        read = getattr(connection, "read_clipboard", None)
        if read is None:
            return None
        return await read(OSC52_LETTER[target])

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
