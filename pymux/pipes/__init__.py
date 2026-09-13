"""
Platform specific (Windows+posix) implementations for inter process
communication through pipes between the Pymux server and clients.
"""

from prompt_toolkit.utils import is_windows

from .base import BrokenPipeError, PipeConnection
from .memory import MemoryConnection, connect_in_memory

__all__ = [
    "bind_and_listen_on_socket",
    # In memory, for a server and a client in one process.
    "connect_in_memory",
    "MemoryConnection",
    # Base.
    "PipeConnection",
    "BrokenPipeError",
]


def bind_and_listen_on_socket(socket_name, accept_callback):
    """
    Bind the socket, and answer with the listener that serves it.

    A listener says its `socket_name`, and its `serve()` takes the
    clients. The two are apart because they happen at two times: the
    bind is what names the server and it happens before `daemonize`
    forks, and the accepting needs the loop that the fork's child
    starts. Lillecarl/pymux#87.

    :param accept_callback: Callback is called with a `PipeConnection` as
        argument.
    """
    if is_windows():
        from .win32_server import bind_and_listen_on_win32_socket

        return bind_and_listen_on_win32_socket(socket_name, accept_callback)
    else:
        from .posix import bind_and_listen_on_posix_socket

        return bind_and_listen_on_posix_socket(socket_name, accept_callback)
