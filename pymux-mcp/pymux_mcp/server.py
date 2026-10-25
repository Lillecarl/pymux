"""
The MCP server: two tools, however many operations there are.

A harness carries every tool's definition on every turn, so thirty
tools would cost thirty definitions each time. Here there are two:
`pymux` runs one operation by name, and `pymux_describe` lists the
operations or answers one's JSON Schema, which an agent asks for only
when it needs it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic
from libpymux import CommandError, Server, ServerNotRunning
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .api import OPERATIONS

__all__ = ["build", "describe", "render"]

INSTRUCTIONS = """\
Drive the pymux terminal multiplexer: sessions, windows, panes, and jobs
that run without a pane. Call `pymux` with an operation and its
arguments; `pymux_describe` lists the operations, and with one name
answers its arguments as JSON Schema. Start with the operation `tree`.
"""

#: The errors an agent can act on. Anything else is a bug in this server
#: and stays a traceback.
_ANSWERABLE = (LookupError, CommandError, ServerNotRunning, pydantic.ValidationError)


def render(result: Any) -> str:
    "An operation's answer as the text the agent reads: text as it is, anything else as JSON."
    if isinstance(result, str):
        return result
    if result is None:
        return "ok"
    return json.dumps(result, ensure_ascii=False)


def describe(op: str | None = None) -> str:
    "Every operation with its summary, or one operation in full."
    if op is None:
        return "\n".join("%s: %s" % (name, found.summary) for name, found in sorted(OPERATIONS.items()))
    found = OPERATIONS.get(op)
    if found is None:
        raise ToolError("no operation %r; pymux_describe with no op lists them" % (op,))
    return json.dumps({"op": op, "description": found.description, "arguments": found.schema()})


def build(server_for: Callable[[], Awaitable[Server]]) -> FastMCP:
    """
    The MCP server, asking `server_for` which pymux server each call
    acts on. It asks every call, so a pymux server that restarted is
    found again.
    """
    # At INFO the SDK writes a line to stderr for every request.
    mcp = FastMCP("pymux", instructions=INSTRUCTIONS, log_level="WARNING")

    async def pymux(op: str, args: dict[str, Any] | None = None) -> str:
        "Run one pymux operation. `pymux_describe` lists them; start with `tree`."
        try:
            return render(await OPERATIONS.call(await server_for(), op, args or {}))
        except _ANSWERABLE as error:
            raise ToolError(str(error)) from error

    async def pymux_describe(op: str | None = None) -> str:
        "List pymux's operations, or answer one operation's arguments as JSON Schema."
        return describe(op)

    mcp.add_tool(pymux, structured_output=False)
    mcp.add_tool(pymux_describe, structured_output=False)
    return mcp
