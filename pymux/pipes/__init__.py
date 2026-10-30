"""
Inter process communication through pipes between the Pymux server and
clients: a unix socket, or a pair of queues in one process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from .base import PipeConnection
    from .posix import PosixSocketListener

__all__ = ["bind_and_listen_on_socket"]


def bind_and_listen_on_socket(
    socket_name: str | None, accept_callback: Callable[[PipeConnection], None]
) -> PosixSocketListener:
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
    from .posix import bind_and_listen_on_posix_socket

    return bind_and_listen_on_posix_socket(socket_name, accept_callback)
