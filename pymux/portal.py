"""
A desktop portal for the programs in window panes, so that they draw
light or dark the way the screen in front of the person does.
Lillecarl/pymux#812.

`wayland-portal` says where it is served:

- `host`: the host's session bus, under the portal's own name, while
  nobody holds that name and no real portal can be started for it.
  A program keeps every other service of the host: `systemctl --user`,
  notifications, the keyring. Where a real portal runs, or the bus can
  start one, pymux leaves the name alone and serves nothing.
- `private`: a dbus-daemon of this server's own, whose address only the
  programs `pymux wl` starts are given. It works beside a real portal,
  and those programs lose the host's bus: `systemctl --user` and
  `notify-send` find nothing there. A terminal pane keeps the host's.
- `off`.

`Terminals.sync_color_scheme` decides the scheme, from the client used
last, and a change goes out as `SettingChanged`, so a program switches
while it runs. Linux only, and only with the `wayland` extra, which
carries dbus-fast.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import sys
import tempfile
from enum import IntEnum
from subprocess import DEVNULL, PIPE
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xml_escape

import anyio
import anyio.abc
from anyio.streams.buffered import BufferedByteReceiveStream

from pymux.audio import SOCKET_PATH_MAX, dies_with_this_process
from pymux.colors import theme_color_base
from pymux.enums import WaylandPortal
from pymux.nearest import oklab

if TYPE_CHECKING:
    from dbus_fast.aio import MessageBus
    from pyte.colors import Color

    from pymux.main import Pymux
    from pymux.portal_settings import Settings

logger = logging.getLogger(__name__)

#: How long a private bus may take to say its address, and to stop.
START_SECONDS = 10.0
STOP_SECONDS = 2.0

#: How soon a portal that failed is tried again.
RETRY_SECONDS = 5.0

#: The private bus's whole configuration. Not `--session`: that reads
#: `/etc/dbus-1/session.conf`, which a machine without a system bus
#: lacks, and its service directories would start the host's services
#: on this bus.
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={path}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class ColorScheme(IntEnum):
    "What `org.freedesktop.appearance color-scheme` holds."

    NO_PREFERENCE = 0
    DARK = 1
    LIGHT = 2


#: OKLab lightness of the middle grey between a light and a dark background.
MIDDLE_LIGHTNESS = 0.5


def color_scheme_of(background: Color | None, swapped: bool) -> ColorScheme:
    "Dark or light, by a background's lightness; flipped when the theme is drawn swapped."
    if background is None:
        return ColorScheme.NO_PREFERENCE
    dark = (oklab(background)[0] < MIDDLE_LIGHTNESS) != swapped
    return ColorScheme.DARK if dark else ColorScheme.LIGHT


def theme_background(name: str) -> Color | None:
    "The background a theme paints, or `None` for a name nobody offers."
    try:
        return theme_color_base(name).defaults["background"]
    except KeyError:
        return None


def available() -> bool:
    "Whether a portal can be served here at all."
    if sys.platform != "linux":
        return False
    try:
        import dbus_fast  # noqa: F401
    except ImportError:
        return False
    return True


def bus_path_of(server_socket: str | None) -> str:
    """
    Where a private bus listens: beside the server's own socket, or in
    a temporary directory when there is none or that path is too long.
    """
    if server_socket:
        directory, name = os.path.split(server_socket)
        path = os.path.join(directory, "dbus-%s.sock" % (name,))
        if len(path) < SOCKET_PATH_MAX:
            return path
    return os.path.join(tempfile.mkdtemp(prefix="pymux-dbus-"), "bus")


class Portal:
    "The portal of one pymux server, while `wayland-portal` is not off."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.scheme = ColorScheme.NO_PREFERENCE
        #: The bus address `pymux wl` programs are given, while the private bus runs.
        self.private_address: str | None = None
        #: Whether an upgrade stopped it, which `resume` undoes.
        self.paused = False
        self._settings: Settings | None = None
        self._process: anyio.abc.Process | None = None
        self._changed = anyio.Event()
        self._scope: anyio.CancelScope | None = None

    def scheme_is(self, scheme: ColorScheme) -> None:
        "The scheme the person sees now; a change reaches every program."
        if scheme == self.scheme:
            return
        self.scheme = scheme
        if self._settings is not None:
            self._settings.scheme_changed()

    def option_changed(self) -> None:
        "`wayland-portal` moved: stop, and start again as it says."
        self._changed.set()
        if self._scope is not None:
            self._scope.cancel()

    def pids(self) -> list[int]:
        "The private bus, while it runs. `list-processes` names it."
        return [] if self._process is None else [self._process.pid]

    async def pause(self) -> None:
        """
        Stop the private bus for `upgrade-server`, and wait until it has
        gone: the exec keeps this pid, and the new image would never reap
        it. The window panes, the only programs that use it, end there too.
        """
        self.paused = True
        process = self._process
        self.option_changed()
        if process is not None:
            with anyio.move_on_after(STOP_SECONDS):
                await process.wait()

    def resume(self) -> None:
        "An upgrade that failed: serve again."
        self.paused = False
        self._changed.set()

    async def keep(self) -> None:
        "Serve whenever the option allows. Never returns."
        while True:
            mode = self.pymux.options.wayland_portal
            failed = False
            if mode != WaylandPortal.OFF and not self.paused and available():
                with anyio.CancelScope() as self._scope:
                    try:
                        await self._serve(mode)
                    except Exception:
                        logger.exception("The portal stopped.")
                        failed = True
                self._scope = None
            with anyio.move_on_after(RETRY_SECONDS if failed else math.inf):
                await self._changed.wait()
            self._changed = anyio.Event()

    async def _serve(self, mode: WaylandPortal) -> None:
        if mode != WaylandPortal.PRIVATE:
            await self._serve_on(None)
            return
        async with anyio.create_task_group() as tasks:
            address = await tasks.start(self._run_bus)
            try:
                await self._serve_on(address)
            finally:
                tasks.cancel_scope.cancel()

    async def _serve_on(self, address: str | None) -> None:
        "Hold the portal's name on the bus at `address`, the host's when `None`."
        from dbus_fast import NameFlag
        from dbus_fast.aio import MessageBus

        from pymux.portal_settings import BUS_NAME, OBJECT_PATH, Settings

        try:
            bus = await MessageBus(bus_address=address).connect()
        except (OSError, ValueError) as e:
            # A machine with no session bus: nothing to serve on.
            logger.info("No session bus for the portal: %s", e)
            return
        try:
            if address is None and await _a_real_portal_can_start(bus, BUS_NAME):
                logger.info("A real portal starts on this bus; pymux serves none.")
                return
            settings = Settings(lambda: int(self.scheme))
            bus.export(OBJECT_PATH, settings)
            # In the queue when somebody holds the name: another pymux
            # server, whose leaving hands it here. A real portal that asks
            # to replace it may.
            reply = await bus.request_name(BUS_NAME, NameFlag.ALLOW_REPLACEMENT)
            self._settings = settings
            logger.info("Portal on the %s bus: %s.", "private" if address else "host", reply.name.lower())
            await bus.wait_for_disconnect()
        finally:
            self._settings = None
            bus.disconnect()

    async def _run_bus(self, *, task_status: anyio.abc.TaskStatus[str] = anyio.TASK_STATUS_IGNORED) -> None:
        "A dbus-daemon for `private`, for as long as the portal serves on it."
        daemon = shutil.which("dbus-daemon")
        if daemon is None:
            raise RuntimeError("wayland-portal private needs dbus-daemon on the PATH")
        path = bus_path_of(self.pymux.socket_name)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        work = tempfile.mkdtemp(prefix="pymux-dbus-conf-")
        config = os.path.join(work, "bus.conf")
        with open(config, "w") as out:
            out.write(BUS_CONFIG.format(path=xml_escape(path)))
        argv = [daemon, "--config-file=%s" % (config,), "--nofork", "--nopidfile", "--nosyslog", "--print-address=1"]
        process = await anyio.open_process(dies_with_this_process(argv), stdin=DEVNULL, stdout=PIPE, stderr=None)
        self._process = process
        try:
            assert process.stdout is not None
            with anyio.fail_after(START_SECONDS):
                address = (await BufferedByteReceiveStream(process.stdout).receive_until(b"\n", 4096)).decode()
            self.private_address = address
            task_status.started(address)
            code = await process.wait()
            raise RuntimeError("dbus-daemon ended with %s" % (code,))
        finally:
            self.private_address = None
            self._process = None
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(STOP_SECONDS, shield=True):
                    await process.wait()
                if process.returncode is None:
                    process.kill()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            shutil.rmtree(work, ignore_errors=True)


async def _a_real_portal_can_start(bus: MessageBus, name: str) -> bool:
    """
    Whether the bus can start a real portal for `name`. Holding the name
    first would keep it from ever starting, on a desktop that has one.
    """
    from dbus_fast import Message

    reply = await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="ListActivatableNames",
        )
    )
    return reply.body is not None and name in reply.body[0]
