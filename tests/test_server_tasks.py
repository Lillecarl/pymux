"""
Tests for the background work of a client connection.

**The work of a connection lives in a scope of its own.** `serve()`
opens a task group, reads the client until the connection ends, and
cancels what is left. So a task cannot outlive the client that asked
for it, and it cannot be collected while it runs.

That last part is what this file started as. asyncio holds only a weak
reference to a task, so a task nobody else holds may be collected while
it is still pending. The loop reports that as an exception, and
prompt_toolkit answers an exception in the loop by leaving the
alternate screen, printing the traceback and asking for a key press. A
pymux server has no terminal of its own and nobody to press a key, so
that answer fails and reports again: one lost task repainted the
terminal of every client without end.

**What a failed task must not do is take anything else down.** In a
task group an exception cancels the siblings and goes up, and up from a
connection is every other client of the server. The work a connection
spawns answers one client, so it is logged there and the connection
lives on. Lillecarl/pymux#87.
"""

import inspect
import logging

import anyio

# pymux's own, and not the builtin of the same name. The read loop of a
# connection ends on this one and logs anything else.
from pymux.pipes import BrokenPipeError
from pymux.server import ServerConnection


class FakePipe:
    "A connection that delivers nothing, and ends when it is closed."

    def __init__(self):
        self.closed = False
        self._gone = anyio.Event()

    async def read(self):
        await self._gone.wait()
        raise BrokenPipeError

    async def write(self, data):
        pass

    def close(self):
        self.closed = True
        self._gone.set()


class FakePymux:
    "What a `ServerConnection` asks its server for, and nothing else."

    def __init__(self, tasks=None):
        self.tasks = tasks
        self.removed = []
        self.color_base_syncs = 0

    def serve_connection(self, connection):
        # No task group means a connection that is built to be asked
        # something and never reads its pipe. `test_colors.py` makes
        # those: a terminal reply arrives, and the connection answers.
        if self.tasks is not None:
            self.tasks.start_soon(connection.serve)

    def remove_client(self, connection):
        self.removed.append(connection)

    def sync_color_bases(self):
        self.color_base_syncs += 1


async def _until(question, seconds: float = 2.0) -> None:
    with anyio.fail_after(seconds):
        while not question():
            await anyio.sleep(0)


async def _connection(tasks) -> ServerConnection:
    "A connection that is already serving."
    pymux = FakePymux(tasks)
    connection = ServerConnection(pymux, FakePipe())
    await _until(lambda: connection._tasks is not None)
    return connection


async def test_the_work_of_a_connection_is_in_a_scope():
    async with anyio.create_task_group() as tasks:
        connection = await _connection(tasks)

        assert connection._tasks is not None, "the connection never served"
        assert connection._tasks is not tasks, "it shares the server's scope"

        connection._close_connection()


async def test_closing_a_connection_ends_its_work():
    "No task of a closed connection is left running."
    ended = []

    async def forever():
        try:
            await anyio.sleep(60)
        finally:
            ended.append(True)

    async with anyio.create_task_group() as tasks:
        connection = await _connection(tasks)
        connection._spawn(forever())
        await _until(lambda: True)

        connection._close_connection()
        await _until(lambda: ended == [True])


async def test_a_failed_task_says_what_it_raised(caplog):
    """
    Nothing awaits a spawned task, so its exception is retrieved by
    nobody. asyncio said so when the task was collected, which is some
    time later and names no cause -- and the work a connection spawns
    is the work that reads a client and answers it, so the symptom was
    a client that stopped responding for a reason nothing printed.
    """

    async def fails():
        raise RuntimeError("the reading task fell over")

    async with anyio.create_task_group() as tasks:
        connection = await _connection(tasks)

        with caplog.at_level(logging.ERROR):
            connection._spawn(fails())
            await _until(lambda: "the reading task fell over" in caplog.text)

        connection._close_connection()


async def test_a_failed_task_leaves_the_connection_reading():
    "One task that fell over is not the end of the client it serves."

    async def fails():
        raise RuntimeError("this one is on its own")

    async with anyio.create_task_group() as tasks:
        connection = await _connection(tasks)

        connection._spawn(fails())
        await anyio.sleep(0.01)

        assert connection._tasks is not None, "the connection went with it"
        assert not connection.pipe_connection.closed

        connection._close_connection()


async def test_a_task_spawned_on_a_closed_connection_says_so(caplog):
    "Nothing would run it, so it is not quietly taken."

    async def nothing():
        return None

    async with anyio.create_task_group() as tasks:
        connection = await _connection(tasks)
        connection._close_connection()
        await _until(lambda: connection._tasks is None)

        with caplog.at_level(logging.WARNING):
            connection._spawn(nothing())

        assert "does not serve" in caplog.text, caplog.text


def test_application_does_not_take_over_exception_handler():
    """
    prompt_toolkit turns an exception in the event loop into a prompt.
    Nothing can answer that prompt on a server, so the server keeps the
    plain handler, which logs.
    """
    source = inspect.getsource(ServerConnection._create_app)
    assert "set_exception_handler=False" in source
