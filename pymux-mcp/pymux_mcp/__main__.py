"""
`pymux-mcp`: serve MCP on stdio, or print the OpenAPI document.

A harness starts it as a stdio MCP server. With no `--socket` each call
acts on the server whose pane this runs in, as `$PYMUX` names it, and
outside a pane on the one pymux server that is running, found again
every time.
The caller is this process: its environment and directory travel with
every command, so a job starts where the harness started this, and the
agent session id the harness put in the environment stamps it.
"""

from __future__ import annotations

import argparse
import json
import sys

from libpymux import Server

from . import __version__
from .openapi import document
from .server import build


def main() -> None:
    parser = argparse.ArgumentParser(prog="pymux-mcp", description="Drive a pymux server over MCP, on stdio.")
    parser.add_argument("--socket", metavar="<path>", help="The server's socket. The one running server otherwise.")
    parser.add_argument("--openapi", action="store_true", help="Print the operations as OpenAPI 3.1, and exit.")
    arguments = parser.parse_args()

    if arguments.openapi:
        sys.stdout.write(json.dumps(document(__version__), indent=2) + "\n")
        return

    async def server_for() -> Server:
        if arguments.socket:
            return Server(arguments.socket)
        enclosing = Server.enclosing()
        if enclosing is not None:
            return enclosing[0]
        return await Server.first()

    build(server_for).run("stdio")


if __name__ == "__main__":
    main()
