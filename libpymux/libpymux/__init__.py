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

import logging

from .connection import (
    CommandError,
    CommandResult,
    Connection,
    ServerNotRunning,
    quote,
)
from .objects import Pane, Server, Session, Window
from .sockets import socket_paths

# A library writes nothing to the terminal of a program that did not ask
# for it. Without a handler, `logging` sends a record to `sys.stderr`
# through its last resort, and this library is imported by pymux itself:
# a warning on stderr lands on top of the frame a pane is drawing.
# `pymux/log.py` gives this logger the file handler of the server.
logging.getLogger(__name__).addHandler(logging.NullHandler())

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
