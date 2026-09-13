"""
Tests for the background work of a client connection.

asyncio holds only a weak reference to a task, so a task that nobody
else holds may be collected while it is still pending. The loop reports
that as an exception, and prompt_toolkit answers an exception in the
loop by leaving the alternate screen, printing the traceback and asking
for a key press. A pymux server has no terminal of its own and nobody
to press a key, so that answer fails and reports again. One lost task
then repaints the terminal of every client without end.
"""

import asyncio
import inspect
import logging

from pymux.server import ServerConnection


class FakePipe:
    "A connection that never delivers anything."

    def __init__(self):
        self.closed = False

    async def read(self):
        await asyncio.Event().wait()

    async def write(self, data):
        pass

    def close(self):
        self.closed = True


class FakePymux:
    def __init__(self):
        self.removed = []
        self.color_base_syncs = 0

    def remove_client(self, connection):
        self.removed.append(connection)

    def sync_color_bases(self):
        self.color_base_syncs += 1


def make_connection():
    return ServerConnection(FakePymux(), FakePipe())


def test_reading_task_is_held():
    async def check():
        connection = make_connection()
        assert connection._tasks, "the reading task is not held anywhere"
        connection._close_connection()

    asyncio.run(check())


def test_closing_connection_stops_its_work():
    "No task of a closed connection is left pending."

    async def check():
        connection = make_connection()
        tasks = list(connection._tasks)
        connection._close_connection()
        await asyncio.sleep(0)
        for task in tasks:
            assert task.cancelled() or task.done()

    asyncio.run(check())


def test_finished_task_is_let_go():
    "The set must not grow with every packet that is sent."

    async def check():
        connection = make_connection()
        before = len(connection._tasks)

        async def nothing():
            return None

        connection._spawn(nothing())
        assert len(connection._tasks) == before + 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(connection._tasks) == before
        connection._close_connection()

    asyncio.run(check())


def test_application_does_not_take_over_exception_handler():
    """
    prompt_toolkit turns an exception in the event loop into a prompt.
    Nothing can answer that prompt on a server, so the server keeps the
    plain handler, which logs.
    """
    source = inspect.getsource(ServerConnection._create_app)
    assert "set_exception_handler=False" in source


async def test_a_failed_task_says_what_it_raised(caplog):
    """
    Nothing awaits a spawned task, so its exception is retrieved by
    nobody. asyncio says so when the task is collected, which is some
    time later and names no cause -- and the work a connection spawns is
    the work that reads a client and answers it, so the symptom was a
    client that stopped responding for a reason nothing printed.
    Lillecarl/pymux#87.
    """
    connection = make_connection()
    # The connection is already reading, and that task stays pending.
    before = len(connection._tasks)

    async def fails():
        raise RuntimeError("the reading task fell over")

    with caplog.at_level(logging.ERROR):
        connection._spawn(fails())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "the reading task fell over" in caplog.text, caplog.text
    assert len(connection._tasks) == before, "the failed task is still held"
    connection._close_connection()
