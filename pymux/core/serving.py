"""
How clients reach a server: the socket it listens on, the listener an
upgrade hands over, and the two ways a server runs, on its own or with
one terminal in the same process.
"""

from __future__ import annotations

import contextvars
import errno
import gc
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

import anyio

from .. import introspect
from ..client.memory import MemoryClient
from ..connection.server import ServerConnection
from ..log import logger
from ..pipes import bind_and_listen_on_socket
from ..pipes.memory import connect_in_memory

if TYPE_CHECKING:
    from prompt_toolkit.output import ColorDepth

    from ..pipes.base import PipeConnection
    from .main import Pymux

__all__ = ["Serving"]


@contextmanager
def freeze_what_started() -> Iterator[None]:
    """
    Keep the heap the server started with out of every later scan, while it serves.

    A full collection scans every tracked object. On dynhetz the live
    server tracked 216 000 after an upgrade, and each full collection
    held the loop for up to 66 ms: four frames at 60 Hz. What exists
    once the modules are imported and a snapshot is loaded mostly lives
    as long as the server. A frozen object that dies is still freed by
    its reference count; only a cycle among frozen ones stays.

    The way out thaws and collects. A frozen cycle is otherwise
    finalized in interpreter teardown, where an import fails and a
    `__del__` prints on the terminal a detach leaves: every
    `check_detach_ends_client` run did. Lillecarl/pyterm#1016.
    """
    gc.collect()
    gc.freeze()
    logger.info("Froze %d objects out of the cycle collector.", gc.get_freeze_count())
    try:
        yield
    finally:
        gc.unfreeze()
        gc.collect()


def _listens(sock: socket.socket) -> bool:
    """
    Whether a stream socket listens.

    Linux says so through `SO_ACCEPTCONN`; macOS has no such option for
    a unix socket. There a `listen` answers instead: it changes nothing
    on a socket that listens already, and fails on a connected one.
    """
    try:
        return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    except OSError as error:
        if error.errno != errno.ENOPROTOOPT:
            raise
    try:
        sock.listen()
    except OSError:
        return False
    return True


class Serving:
    "Listens for clients and runs the server until it stops."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    def serve_listener(self) -> None:
        "Take clients on `listener`, in the server's task group."
        if self.pymux.tasks is None or self.pymux.listener is None:
            raise RuntimeError("A listener is served inside `Pymux.running`, once there is one.")
        self.pymux.tasks.start_soon(self.pymux.listener.serve)

    def serve_connection(self, connection: ServerConnection) -> None:
        """
        Put a connection's own read loop in the server's task group.

        A `ServerConnection` is built in a sync callback -- the accept
        of a socket, or the harness -- and everything it then does is
        async. This is the one line between the two.
        """
        if self.pymux.tasks is None:
            raise RuntimeError("A connection was made outside `Pymux.running`, so nothing would read it.")
        self.pymux.tasks.start_soon(connection.serve)

    def listen_on_socket(self, socket_name: str | None = None) -> str:
        """
        Bind the socket that clients connect to, and say its name.

        **The clients are taken later.** The bind has to happen before
        `daemonize` forks, because the name is what the client that
        started this server connects to, and the accepting needs the
        loop that the child starts. `running()` runs the listener.
        """
        self.pymux.listener = bind_and_listen_on_socket(socket_name, self.accept_connection)
        self.pymux.socket_name = self.pymux.listener.socket_name

        # Set session_name according to socket name.
        #        if '.' in self.socket_name:
        #            self.session_name = self.socket_name.rpartition('.')[-1]

        # `bind_and_listen_on_socket` already said it. Two lines were
        # half the log of a server that started well.
        return self.pymux.socket_name

    def adopt_listener(self, fd: int) -> None:
        """
        Serve the listening socket the server before an upgrade bound.

        `socket_name` is the snapshot's, and the fd has to be listening
        on that name: a number is only what the old server wrote down.
        Binding again is no answer, because this process still listens
        there. Lillecarl/pyterm#410.
        """
        from ..pipes.posix import BACKLOG, PosixSocketListener

        sock = socket.socket(fileno=fd)
        if sock.family != socket.AF_UNIX or not _listens(sock):
            sock.detach()
            raise OSError(errno.ENOTSOCK, "fd %d is not a listening unix socket" % fd)
        socket_name = self.pymux.socket_name
        if socket_name is None or sock.getsockname() != socket_name:
            sock.detach()
            raise OSError(errno.EADDRNOTAVAIL, "fd %d does not listen on %r" % (fd, socket_name))
        # The backlog is the socket's, and a server bound by an older
        # build carries that build's number across every exec.
        sock.listen(BACKLOG)
        self.pymux.listener = PosixSocketListener(socket_name, sock, self.accept_connection)

    def accept_connection(self, pipe_connection: PipeConnection) -> None:
        # We have to create a new `context`, because this will be the scope for
        # a new prompt_toolkit.Application to become active.
        #
        # **A client that arrives here may not always attach.** An
        # integrated server draws on the terminal it runs in, and
        # this socket is for commands. The question is asked at the
        # moment a client arrives, because the route is chosen after
        # the bind. Lillecarl/pyterm#161.
        may_attach = not self.pymux._serves_one_terminal
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(self.pymux, pipe_connection, may_attach=may_attach))

        self.pymux.connections.append(connection)

    def run_server(self, resumed: Callable[[], Awaitable[None]] | None = None):
        """
        Serve on the socket until the server stops.

        `resumed` runs first inside `running()`: an upgrade's new build
        loads its snapshot there (`pymux.core.upgrade.resume`). An exception
        from it ends the server with a crash report, as any other does.
        """

        # Ignore keyboard. (When people run "pymux server" and press Ctrl-C.)
        # Pymux has to be terminated by termining all the processes running in
        # its panes.
        def handle_sigint(*a: object) -> None:
            print("Ignoring keyboard interrupt.")

        signal.signal(signal.SIGINT, handle_sigint)

        self.pymux.server_starts()

        async def serve() -> None:
            try:
                # This server forks its programs, and an upgrade execs in
                # place to keep them. Lillecarl/pyterm#738.
                async with self.pymux.running():
                    if resumed is not None:
                        await resumed()
                    with freeze_what_started():
                        await self.pymux.done.wait()
            finally:
                # `stop()` is what set `done`, so this is usually a
                # second call that finds nothing. It is here for the
                # way out that nobody asked for: a pane that still runs
                # holds a `waitpid` in the executor, and the loop waits
                # for that thread when it closes.
                self.pymux.stop()

        # Run eventloop.
        started = time.monotonic()
        try:
            anyio.run(serve)
            # A log that ends without this line, a crash or "Ended by"
            # ended with SIGKILL or with no Python left to say it.
            # Lillecarl/pyterm#1002.
            logger.info("The server stopped after %.1f s.", time.monotonic() - started)
        except BaseException as error:
            # A daemon's stderr is /dev/null, so a crash that is not
            # written down is a crash nobody can read. The report goes
            # beside the log, and the log's last line names it.
            # Lillecarl/pyterm#738.
            report = introspect.write_crash_report(error, started)
            logger.critical("Pymux crashed: %r. The report is %s.", error, report, exc_info=error)
            raise

        finally:
            # Clean up socket.
            self._remove_socket()

    def _remove_socket(self) -> None:
        "Take away the socket file of this server, if it bound one."
        if self.pymux.listener is not None:
            self.pymux.listener.close()
        if not self.pymux.socket_name:
            return
        with suppress(OSError):
            os.remove(self.pymux.socket_name)

    def run_integrated(
        self,
        color_depth: ColorDepth | None,
        detach_other_clients: bool = False,
        chosen_name: str | None = None,
        hang_up_others: bool = False,
        read_only: bool = False,
    ):
        """
        Run the server and one client in this process.

        The client reaches the server through a pair of queues, not
        through a socket, so it reaches the server that this call
        started and nothing else. Both halves of the protocol run, so
        this route proves what the socket route proves.

        A socket is bound as well when one was asked for. Nothing of
        the user interface reads it. It is there so that
        `pymux -S <socket> <command>` and libpymux reach this server.

        **A client cannot attach over that socket.** This server draws
        on the terminal it runs in, and a second terminal would hold
        half a session that the first one ends: `ctrl+b d` here takes
        the process with it. So `start-gui` on that socket is answered
        with `server.CANNOT_ATTACH` and the connection closes, and
        commands are what the socket serves. Lillecarl/pyterm#161.

        The call returns when the server closes the connection: the
        last pane exited, somebody ran `kill-server`, or the client
        detached. The process ends with it, because both halves are in
        it.
        """
        self.pymux._serves_one_terminal = True
        self.pymux.server_starts()

        #: The client, kept so that the hangup of `attach -x` can be
        #: sent after the loop has gone. Lillecarl/pyterm#349.
        attached: list[MemoryClient] = []

        async def run() -> None:
            try:
                async with self.pymux.running():
                    with freeze_what_started():
                        await self._serve_one_terminal(
                            attached, color_depth, detach_other_clients, chosen_name, hang_up_others, read_only
                        )
            finally:
                # The client has gone, and the server is in this
                # process, so nothing is left for the session to live
                # on. `ctrl+b d` means quit here, and that is the one
                # honest reading.
                #
                # It is also what lets the process exit at all, and
                # **it has to happen inside the loop**: a pane that is
                # still running holds a `waitpid` in the executor of
                # the loop, and `anyio.run` waits for the executor
                # threads on its way out. Without this the client put
                # the terminal back and the person was left looking at
                # their shell with no prompt. Lillecarl/pyterm#111, and
                # the place it is written is Lillecarl/pyterm#89.
                self.pymux.stop()

        try:
            anyio.run(run)
        finally:
            self._remove_socket()
            # After the loop, so the terminal is back before the
            # process that started this one hears about it.
            for client in attached:
                client.hang_up_the_parent()

    async def _serve_one_terminal(
        self,
        attached: list[MemoryClient],
        color_depth: ColorDepth | None,
        detach_other_clients: bool,
        chosen_name: str | None,
        hang_up_others: bool,
        read_only: bool,
    ) -> None:
        "The integrated route's one client, over a pair of queues, until it leaves."
        server_end, client_end = connect_in_memory()

        # A context of its own, the same as for a client that arrives
        # over the socket. A `prompt_toolkit.Application` becomes
        # active in it.
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(self.pymux, server_end))
        self.pymux.connections.append(connection)

        client = MemoryClient(client_end)
        # One process reads the file twice: as the server, for
        # everything, and as the client, for the lines that are about
        # this terminal. Lillecarl/pyterm#225.
        client.config_file = self.pymux.source_file
        client.chosen_name = chosen_name
        client.hang_up_others = hang_up_others
        # `-r` here is a watcher of a session nobody else can reach,
        # which is a thin thing to want; it is passed on all the same,
        # because the flag means the same wherever it is written and
        # `ctrl+b d` is still the way out. Lillecarl/pyterm#469.
        client.read_only = read_only
        attached.append(client)
        await client.attach(
            detach_other_clients=detach_other_clients,
            color_depth=color_depth,
        )
