"""
The one move off the holder, as Carl's server of 2026-10-22 makes it.

That server forks every pane through the holder, and `upgrade-server`
starts the new build beside itself with `resume-server PATH --ready-fd N`.
The new build takes every program from the holder, says `ok`, and ends
the holder with SIGTERM, which leaves the programs running. Here the old
server is this process, and the new build a real one. Delete this with
the holder (stage E of Lillecarl/pymux#736).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import anyio
import apsw
from ptyhost.held import HeldBackend
from session import create_session
from test_jobs_over_the_socket import CLI_TIMEOUT, cli
from test_upgrade import _gone

from pymux import snapshot


async def _answer(sock, *args) -> str:
    done = await anyio.to_thread.run_sync(lambda: cli(sock, *args))
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode()


async def _until(what, complaint) -> None:
    "Wait for `what`. `complaint` is a string, or what makes one once the wait failed."
    deadline = time.monotonic() + CLI_TIMEOUT
    while not await what():
        assert time.monotonic() < deadline, complaint() if callable(complaint) else complaint
        await anyio.sleep(0.1)


async def test_the_move_off_the_holder_keeps_every_program(tmp_path):
    sock = tmp_path / "old.sock"
    log = tmp_path / "server.log"
    async with create_session(window=None) as (pymux, _state):
        pymux.listen_on_socket(str(sock))
        await pymux.hold()
        assert pymux.holding is not None
        holder = pymux.holding.pid
        await pymux.create_window("sh")
        # The client's arrival made a pane before the holder was there.
        for early in [one for one in snapshot._panes_of(pymux) if not isinstance(one.process.backend, HeldBackend)]:
            early.process.kill()

        async def held_alone() -> bool:
            return all(isinstance(one.process.backend, HeldBackend) for one in snapshot._panes_of(pymux))

        await _until(held_alone, "a pane forked before the holder stayed")
        (pane,) = snapshot._panes_of(pymux)
        backend = pane.process.backend
        assert isinstance(backend, HeldBackend)
        program = backend.pid

        # The old server's half: the snapshot, the pause, and the new
        # build started beside it on the listening socket and a pipe.
        path = tmp_path / "old.sqlite"
        snapshot.save(pymux, path)
        backend.pause_reading()
        # In the shape that server writes: version 26, with no fds.
        db = apsw.Connection(str(path))
        for table, column in (
            ("jobs", "fds"),
            ("jobs", "parent"),
            ("pane_programs", "master_fd"),
            ("pane_programs", "parent"),
        ):
            db.execute("ALTER TABLE %s DROP COLUMN %s" % (table, column))
        db.execute("PRAGMA user_version = 26")
        db.close()
        ready, told = os.pipe()
        try:
            new = await anyio.open_process(
                [sys.executable, "-m", "pymux", "--log", str(log), "resume-server", str(path), "--ready-fd", str(told)],
                pass_fds=(pymux.listener.socket.fileno(), told),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(told)
        try:
            said = b""
            with anyio.fail_after(CLI_TIMEOUT):
                while said != b"ok" and (chunk := await _read(ready)):
                    said += chunk
            assert said == b"ok", log.read_text() if log.exists() else said
            # The old server ends without a word to the holder.
            holding, pymux.holding, pymux.jobs.holding = pymux.holding, None, None
            holding.close()

            await _until(lambda: _gone_soon(holder), "the holder outlived the move")
            assert not _gone(program), "the program went with the holder"
            panes = await _answer(sock, "list-panes", "-F", "#{pane_pid}")
            assert panes.split() == [str(program)]
            await _answer(sock, "send-keys", "echo mar''ker", "Enter")
            await _until(lambda: _shows(sock, "marker"), lambda: "the program never answered: %s" % (log.read_text(),))
            assert "Took 1 panes over from the holder, which ends." in log.read_text()

            # Not its child, and still its program: it ends with the server.
            await anyio.to_thread.run_sync(lambda: cli(sock, "kill-server"))
            await _until(lambda: _gone_soon(program), "the program outlived kill-server")
        finally:
            os.close(ready)
            if new.returncode is None:
                new.kill()
            await new.wait()


async def _read(fd: int) -> bytes:
    await anyio.wait_readable(fd)
    return os.read(fd, 4096)


async def _gone_soon(pid: int) -> bool:
    return _gone(pid)


async def _shows(sock, text: str) -> bool:
    return text in await _answer(sock, "capture-pane", "-p")
