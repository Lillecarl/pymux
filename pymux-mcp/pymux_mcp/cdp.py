"""
The Chrome DevTools Protocol over one WebSocket: requests matched to
their replies by id, and events to whoever listens for them.

Ported from cdp-mcp. Lillecarl/pymux#822.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

__all__ = ["CDPError", "Connection", "Page", "connected"]

type JsonObject = dict[str, Any]
type Listener = Callable[[JsonObject], None]

TIMEOUT: Final = 30.0


class CDPError(RuntimeError):
    "An error the browser answered, or a connection that went away."


@dataclass
class _Pending:
    done: anyio.Event = field(default_factory=anyio.Event)
    result: JsonObject | None = None
    error: CDPError | None = None


class Connection:
    "One browser's WebSocket. `connected` opens it; nothing else does."

    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._listeners: dict[tuple[str, str | None], list[Listener]] = {}

    async def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = TIMEOUT,
    ) -> JsonObject:
        self._next_id += 1
        request_id = self._next_id
        pending = self._pending[request_id] = _Pending()
        message: JsonObject = {"id": request_id, "method": method, "params": dict(params or {})}
        if session_id is not None:
            message["sessionId"] = session_id
        try:
            await self._socket.send(json.dumps(message))
            with anyio.fail_after(timeout):
                await pending.done.wait()
        except TimeoutError:
            raise CDPError("%s: no answer in %gs" % (method, timeout)) from None
        finally:
            self._pending.pop(request_id, None)
        if pending.error is not None:
            raise pending.error
        return pending.result or {}

    def listen(self, method: str, listener: Listener, session_id: str | None = None) -> Callable[[], None]:
        "Call `listener` with each event `method` of one session; answers what stops it."
        listeners = self._listeners.setdefault((method, session_id), [])
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    async def read(self) -> None:
        error = CDPError("the browser closed the connection")
        try:
            async for raw in self._socket:
                self._dispatch(json.loads(raw))
        except ConnectionClosed:
            pass
        finally:
            for pending in self._pending.values():
                pending.error = error
                pending.done.set()

    def _dispatch(self, message: JsonObject) -> None:
        if "id" in message:
            pending = self._pending.get(message["id"])
            if pending is None:
                return
            error = message.get("error")
            if error is not None:
                pending.error = CDPError("%s (%s)" % (error.get("message", ""), error.get("code", "")))
            else:
                pending.result = message.get("result", {})
            pending.done.set()
            return
        key = (message.get("method", ""), message.get("sessionId"))
        for listener in list(self._listeners.get(key, ())):
            listener(message.get("params", {}))


@asynccontextmanager
async def connected(url: str) -> AsyncIterator[Connection]:
    "A connection to the browser at `url`, open for the block."
    # No size limit: a screenshot is one message of megabytes.
    try:
        async with connect(url, max_size=None, ping_interval=None) as socket, anyio.create_task_group() as tasks:
            connection = Connection(socket)
            tasks.start_soon(connection.read)
            try:
                yield connection
            finally:
                tasks.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        # The task group wraps what the block raised. The caller asked for
        # the block, and an agent reads a `ValueError`, not a group.
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise


class Page:
    "One tab, attached to through a connection."

    def __init__(self, connection: Connection, session_id: str, target_id: str) -> None:
        self.connection = connection
        self.session_id = session_id
        self.target_id = target_id

    async def send(self, method: str, params: Mapping[str, Any] | None = None, timeout: float = TIMEOUT) -> JsonObject:
        return await self.connection.send(method, params, session_id=self.session_id, timeout=timeout)

    def listen(self, method: str, listener: Listener) -> Callable[[], None]:
        return self.connection.listen(method, listener, self.session_id)

    @classmethod
    async def attach(cls, connection: Connection, target_id: str) -> Page:
        attached = await connection.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return cls(connection, attached["sessionId"], target_id)
