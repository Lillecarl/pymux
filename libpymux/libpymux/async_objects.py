"""
The objects of `objects.py`, for a caller that runs a loop.

They run the same operations, from `operations.py`, over an
`AsyncConnection`, and they hold the same fields, from `records.py`.

**A read that asks the server is a method here, never a property.** A
property cannot be awaited, so `server.panes` in `objects.py` is
`await server.panes()` here. A field the object already holds, such as
`pane.width`, stays a property in both.
"""

from __future__ import annotations

# `AsyncServer.list` hides the builtin inside that class.
import builtins
from collections.abc import Sequence

import anyio

from . import operations
from .async_connection import AsyncConnection
from .connection import CommandResult
from .objects import _the_one
from .operations import Ask, Operation, Row, drive_async
from .records import PaneFields, SessionFields, WindowFields
from .sockets import socket_paths
from .streams import PaneStream

__all__ = ["AsyncPane", "AsyncServer", "AsyncSession", "AsyncWindow"]


class AsyncPane(PaneFields):
    "`Pane`, awaited."

    def __init__(self, server: AsyncServer, values: Row) -> None:
        super().__init__(values)
        self.server = server

    async def current_revision(self) -> int:
        "The revision this pane holds now. `Pane.current_revision` says why it matters."
        return await self.server.run(operations.current_revision(self.id))

    async def window(self) -> AsyncWindow | None:
        return self.server._window(await self.server.run(operations.window(self.window_id)))

    async def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        "Send text to the program in this pane. `Pane.send_keys` says what the flags do."
        await self.server.run(operations.send_keys(self.id, text, enter, literal))

    async def send_key(self, name: str) -> None:
        'Send one named key, such as "C-c" or "Escape".'
        await self.server.run(operations.send_key(self.id, name))

    async def capture(self, start: int | None = None, end: int | None = None) -> str:
        return await self.server.run(operations.capture(self.id, start, end))

    async def capture_html(self, start: int | None = None, end: int | None = None) -> str:
        "`Pane.capture_html` says what the markup holds and what it leaves out."
        return await self.server.run(operations.capture(self.id, start, end, html=True))

    async def wait_for_change(self, since: int | None = None, timeout: float | None = None) -> int:
        "`Pane.wait_for_change`: seed `since` with `await current_revision()`."
        return await self.server.run(operations.wait_for_change(self.id, since, timeout))

    def stream(self, writable: bool = False) -> PaneStream:
        return PaneStream(self.server.socket_path, self.id, writable=writable)

    async def clear_history(self) -> None:
        await self.server.run(operations.clear_history(self.id))

    async def select(self) -> None:
        await self.server.run(operations.select_pane(self.id))

    async def kill(self) -> None:
        await self.server.run(operations.kill_pane(self.id))

    async def rename(self, name: str) -> None:
        await self.server.run(operations.rename_pane(self.id, name))

    async def refresh(self) -> AsyncPane:
        row = await self.server.run(operations.pane(self.id))
        if row is None:
            raise LookupError("the pane %s is gone" % (self.id,))
        self._values = row
        return self


class AsyncWindow(WindowFields):
    "`Window`, awaited."

    def __init__(self, server: AsyncServer, values: Row) -> None:
        super().__init__(values)
        self.server = server

    async def panes(self) -> list[AsyncPane]:
        return self.server._panes(await self.server.run(operations.window_panes(self.id)))

    async def active_pane(self) -> AsyncPane | None:
        for pane in await self.panes():
            if pane.active:
                return pane
        return None

    async def split(
        self,
        command: str | None = None,
        vertical: bool = True,
        start_directory: str | None = None,
        select: bool = True,
    ) -> AsyncPane | None:
        "Split this window and return the pane that appears."
        row = await self.server.run(operations.split_window(self.id, command, vertical, start_directory, select))
        return None if row is None else AsyncPane(self.server, row)

    async def select(self) -> None:
        await self.server.run(operations.select_window(self.index))

    async def rename(self, name: str) -> None:
        await self.server.run(operations.rename_window(self.index, name))

    async def kill(self) -> None:
        await self.server.run(operations.kill_window(self.index))

    async def refresh(self) -> AsyncWindow:
        row = await self.server.run(operations.window(self.id))
        if row is None:
            raise LookupError("the window %s is gone" % (self.id,))
        self._values = row
        return self


class AsyncSession(SessionFields):
    "`Session`, awaited."

    def __init__(self, server: AsyncServer, values: Row) -> None:
        super().__init__(values)
        self.server = server

    async def windows(self) -> list[AsyncWindow]:
        found = await self.server.run(operations.session_windows(self.id))
        return [AsyncWindow(self.server, row) for row in found]

    async def active_window(self) -> AsyncWindow | None:
        for window in await self.windows():
            if window.active:
                return window
        return None

    async def panes(self) -> list[AsyncPane]:
        return self.server._panes(await self.server.run(operations.session_panes(self.id)))

    async def new_window(
        self,
        command: str | None = None,
        name: str | None = None,
        start_directory: str | None = None,
        select: bool = True,
    ) -> AsyncWindow | None:
        return self.server._window(
            await self.server.run(operations.new_window(self.name, command, name, start_directory, select))
        )

    async def rename(self, name: str) -> None:
        await self.server.run(operations.rename_session(self.id, name))

    async def kill(self) -> None:
        "End this session. The last one to go ends the server."
        await self.server.run(operations.kill_session(self.id))

    async def refresh(self) -> AsyncSession:
        row = await self.server.run(operations.session(self.id))
        if row is None:
            raise LookupError("the session %s is gone" % (self.id,))
        self._values = row
        return self


class AsyncServer:
    "`Server`, awaited."

    def __init__(self, socket_path: str) -> None:
        self.connection = AsyncConnection(socket_path)

    def __repr__(self) -> str:
        return "AsyncServer(%r)" % (self.socket_path,)

    @property
    def socket_path(self) -> str:
        return self.connection.socket_path

    @classmethod
    async def list(cls) -> list[AsyncServer]:
        "Every pymux server of this user in the default place."
        # `socket_paths` probes each socket with a blocking connect.
        return [cls(path) for path in await anyio.to_thread.run_sync(socket_paths)]

    @classmethod
    async def first(cls) -> AsyncServer:
        "The one server that is running. Raises when there is not one."
        return cls(_the_one(await anyio.to_thread.run_sync(socket_paths)))

    # -- the wire ------------------------------------------------------

    async def cmd(
        self,
        command: str | Sequence[str],
        pane_id: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        "Run one pymux command: anything the command line can do."
        return await self.connection.run(command, pane_id=pane_id, check=check)

    async def run[T](self, operation: Operation[T]) -> T:
        "Run one operation of `operations.py`."
        return await drive_async(operation, self._ask)

    async def _ask(self, ask: Ask) -> CommandResult:
        return await self.cmd(ask.arguments, check=ask.check)

    def _window(self, row: Row | None) -> AsyncWindow | None:
        return None if row is None else AsyncWindow(self, row)

    def _panes(self, found: builtins.list[Row]) -> builtins.list[AsyncPane]:
        return [AsyncPane(self, row) for row in found]

    async def is_alive(self) -> bool:
        return await self.connection.is_alive()

    async def html_stylesheet(self, pane: AsyncPane | None = None) -> str:
        "The stylesheet that `capture_html` is written against."
        return await self.run(operations.html_stylesheet(None if pane is None else pane.id))

    # -- what it holds -------------------------------------------------

    async def info(self) -> Row:
        return await self.run(operations.server_info())

    async def sessions(self) -> builtins.list[AsyncSession]:
        "Every session of this server, oldest first."
        return [AsyncSession(self, row) for row in await self.run(operations.sessions())]

    async def session(self) -> AsyncSession:
        "The oldest session of this server."
        found = await self.sessions()
        if not found:
            raise LookupError("the server has no session")
        return found[0]

    async def session_named(self, name: str) -> AsyncSession | None:
        row = await self.run(operations.session_named(name))
        return None if row is None else AsyncSession(self, row)

    async def new_session(self, name: str, attach: bool = False) -> AsyncSession | None:
        row = await self.run(operations.new_session(name, attach))
        return None if row is None else AsyncSession(self, row)

    async def windows(self) -> builtins.list[AsyncWindow]:
        "Every window of the session a client last looked at."
        return [AsyncWindow(self, row) for row in await self.run(operations.windows())]

    async def panes(self) -> builtins.list[AsyncPane]:
        "Every pane of every window."
        return self._panes(await self.run(operations.panes()))

    async def window(self, index: int) -> AsyncWindow | None:
        return self._window(await self.run(operations.window_at(index)))

    async def pane(self, pane_id: str) -> AsyncPane | None:
        row = await self.run(operations.pane(pane_id))
        return None if row is None else AsyncPane(self, row)

    async def has_session(self, name: str = "") -> bool:
        return await self.run(operations.has_session(name))

    async def kill(self) -> None:
        "End the server and everything in it."
        await self.run(operations.kill_server())
