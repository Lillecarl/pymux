"""
Drive a pymux server from python.

The shape follows libtmux: a server holds sessions, a session holds
windows, a window holds panes. Unlike libtmux, nothing here starts a
program: the wire of pymux is JSON on a unix socket, so this talks to
the server itself.

    from libpymux import Server

    server = Server.first()
    pane = server.session.active_window.active_pane
    pane.send_keys("echo hello")
    print(pane.capture())

`Server.cmd()` runs any pymux command, so nothing the command line can
do is out of reach.
"""
from .connection import (
    CommandError,
    CommandResult,
    Connection,
    ServerNotRunning,
    quote,
    socket_paths,
)
from .objects import Pane, Server, Session, Window

__all__ = [
    "CommandError",
    "CommandResult",
    "Connection",
    "Pane",
    "Server",
    "ServerNotRunning",
    "Session",
    "Window",
    "quote",
    "socket_paths",
]

__version__ = "0.1"
