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

    def remove_client(self, connection):
        self.removed.append(connection)


def make_connection():
    return ServerConnection(FakePymux(), FakePipe())


def test_the_reading_task_is_held():
    async def check():
        connection = make_connection()
        assert connection._tasks, "the reading task is not held anywhere"
        connection._close_connection()

    asyncio.run(check())


def test_closing_the_connection_stops_its_work():
    "No task of a closed connection is left pending."

    async def check():
        connection = make_connection()
        tasks = list(connection._tasks)
        connection._close_connection()
        await asyncio.sleep(0)
        for task in tasks:
            assert task.cancelled() or task.done()

    asyncio.run(check())


def test_a_finished_task_is_let_go():
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


def test_the_application_does_not_take_over_the_exception_handler():
    """
    prompt_toolkit turns an exception in the event loop into a prompt.
    Nothing can answer that prompt on a server, so the server keeps the
    plain handler, which logs.
    """
    source = inspect.getsource(ServerConnection._create_app)
    assert "set_exception_handler=False" in source
