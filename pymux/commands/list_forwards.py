import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing

#: What a client with nothing forwarded says, so that the listing never
#: answers with an empty popup.
NOTHING = "No client is forwarding a port."


def list_forwards(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    List the ports the clients of this server forward.

    **This reads a copy and not the truth.** The server holds no SSH
    connection: each client reports its own table when it changes and
    when a dropped link comes back, and this draws what last arrived.
    A forward that could not open is listed with the reason beside it,
    because that is the question a person has when the port is dead.
    Lillecarl/pymux#436.
    """
    lines = []

    for client in pymux.clients:
        for one in client.connection.forwards:
            lines.append(
                "%s: %s -> %s%s"
                % (
                    client.connection.name,
                    _listen(one),
                    one.get("dest", ""),
                    " (%s)" % (one["error"],) if one.get("error") else "",
                )
            )

    show_listing(pymux, "list-forwards", "\n".join(lines) if lines else NOTHING)


def _listen(one: dict) -> str:
    """
    The listening end, with the flag that made it.

    The port is the one really bound, which differs from the one asked
    for when the person asked for any free port.
    """
    flag = "-L" if one.get("direction") == "local" else "-R"
    return "%s %s:%s" % (flag, one.get("listen_host", ""), one.get("port", ""))


def register(subparsers):
    add_command(subparsers, list_forwards, aliases=("lsf",))
