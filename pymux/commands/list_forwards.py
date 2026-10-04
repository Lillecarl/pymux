from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import show_listing

#: What a client with nothing forwarded says, so that the listing never
#: answers with an empty popup.
NOTHING = "No client is forwarding a port."


def list_forwards(pymux: Pymux, args: argparse.Namespace) -> None:
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
    """
    lines: list[str] = []

    for client in pymux.clients:
        for one in client.connection.forwards:
            notes = []
            if one.get("ours"):
                notes.append("pymux")
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


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, list_forwards, aliases=("lsf",))
