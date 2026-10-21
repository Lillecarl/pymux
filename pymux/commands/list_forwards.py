from __future__ import annotations

import argparse
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from pymux.main import Pymux
    from pymux.server import ServerConnection


from pymux.commands import CommandParser, add_command
from pymux.commands.common import show_listing

#: What a client with nothing forwarded says, so that the listing never
#: answers with an empty popup.
NOTHING = "No client is forwarding a port."

#: How long the listing waits for the clients' fresh tables. A client
#: that does not answer in time is drawn from the copy it sent last.
FRESH_SECONDS = 2.0


def list_forwards(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None] | None:
    """
    List the ports the clients of this server forward.

    **This reads a copy and not the truth.** The server holds no SSH
    connection: each client reports its own table when it changes and
    when a dropped link comes back, and this draws what last arrived.
    A forward that could not open is listed with the reason beside it,
    because that is the question a person has when the port is dead.

    A line pymux opened by itself, for a loopback URL, is marked
    `(pymux)`: it is reaped when idle, so it is not a person's to keep.
    Lillecarl/pymux#436. Lillecarl/pymux#448.

    **The time it has left moves between reports**, so the listing asks
    each client that answers for its table first. One in use shows how
    many connections it carries instead, because its clock has not
    started. Lillecarl/pymux#487.
    """
    asked = [client.connection for client in pymux.clients if client.connection.answers_forwards]
    if asked:
        return _list_fresh(pymux, asked)
    _list(pymux)
    return None


async def _list_fresh(pymux: Pymux, asked: list[ServerConnection]) -> None:
    with anyio.move_on_after(FRESH_SECONDS):
        async with anyio.create_task_group() as tasks:
            for connection in asked:
                tasks.start_soon(connection.fresh_forwards)
    _list(pymux)


def _list(pymux: Pymux) -> None:
    lines: list[str] = []

    for client in pymux.clients:
        for one in client.connection.forwards:
            notes = []
            if one.get("ours"):
                notes.append("pymux")
            if one.get("live"):
                notes.append("in use by %d" % (one["live"],))
            elif one.get("left") is not None:
                notes.append("closes in %s" % (_duration(one["left"]),))
            if one.get("error"):
                notes.append(one["error"])
            lines.append(
                "%s: %s -> %s%s"
                % (
                    client.connection.name,
                    _listen(one),
                    one.get("dest", ""),
                    " (%s)" % (", ".join(notes),) if notes else "",
                )
            )

    show_listing(pymux, "list-forwards", "\n".join(lines) if lines else NOTHING)


def _listen(one: dict[str, Any]) -> str:
    """
    The listening end, with the flag that made it.

    The port is the one really bound, which differs from the one asked
    for when the person asked for any free port.
    """
    flag = "-L" if one.get("direction") == "local" else "-R"
    return "%s %s:%s" % (flag, one.get("listen_host", ""), one.get("port", ""))


def _duration(seconds: float) -> str:
    "`4m05s`, or `12s` under a minute."
    minutes, seconds = divmod(int(seconds), 60)
    return "%dm%02ds" % (minutes, seconds) if minutes else "%ds" % (seconds,)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, list_forwards, aliases=("lsf",))
