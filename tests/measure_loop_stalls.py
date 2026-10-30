"""
How long the event loop stalls while a window pane plays video, and
what it was running when it did. Lillecarl/pymux#632.

mpv plays `testsrc2` at 60 frames a second in a window pane, for one
client at a kitty terminal. A ticker on the loop sleeps `TICK` again
and again and records how late each wake is. Each tick also re-arms
`faulthandler.dump_traceback_later`. faulthandler's watchdog is a C
thread that needs no GIL, so a stall longer than `STALL` is caught
while it lasts, inside a C call that holds the GIL too. A Python
watchdog thread would see only what ran after the stall.

The client is the socket route without the terminal: a socket pair,
the `start-gui` a real client sends, and `cat` reading the other end,
so nothing of the client runs on the server's loop. Each `ROUTES`
entry is one wire a client can ask for.

Each run first blocks the loop on purpose for `CALIBRATION`, so a
report of no stall is one the probe could have broken.

Not a gate, and it judges nothing: the sandbox runs beside other jobs.

    nix build --file . checks.pymux-loop-stalls.run

`PYMUX_STALL_SECONDS` sets how long mpv plays on each route, 20 by
default. `PYMUX_STALL_MS` sets how late a tick is when its stack is
taken, 10 by default.
"""

from __future__ import annotations

import faulthandler
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import anyio
from libpymux.wire import MSGPACK, MSGPACK_ZSTD, encode_json
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import ColorDepth
from session import once

from pymux.main import Pymux
from pymux.pipes.posix import PosixSocketConnection

TICK = 0.005
STALL = float(os.environ.get("PYMUX_STALL_MS") or 10) / 1000
CALIBRATION = 0.030
#: Cells of 10 by 20 pixels: the window pane beside the first pane is
#: about 1600 by 900, the size #632 measured.
SIZE = Size(rows=46, columns=322)
CELL = (10, 20)
SECONDS = float(os.environ.get("PYMUX_STALL_SECONDS") or 20)
OUT = Path(os.environ.get("out") or tempfile.mkdtemp())
#: How many frames of each stall's stack the report keeps, innermost last.
DEPTH = 14

#: What the `start-gui` of each route offers. `frames` is what every
#: pymux client of this build sends: raw pixels in msgpack, perhaps in
#: zstd. Without it the server encodes kitty graphics itself, in JSON.
ROUTES = {
    "zstd_frames": {"wire": [MSGPACK_ZSTD, MSGPACK], "frames": True, "codecs": [], "shows": False},
    "msgpack_frames": {"wire": [MSGPACK], "frames": True, "codecs": [], "shows": False},
    "json_kitty": {},
}

DUMP_HEADER = re.compile(r"^(?:Current thread|Thread) (0x[0-9a-f]+)")
DUMP_FRAME = re.compile(r'^\s+File "([^"]+)", line (\d+) in (\S+)')


def stacks_of_the_loop(dump: str, loop_thread: int) -> list[list[str]]:
    "Each dump's stack of the loop thread, innermost frame last."
    stacks: list[list[str]] = []
    current: list[str] | None = None
    for line in dump.splitlines():
        header = DUMP_HEADER.match(line)
        if header:
            if current is not None:
                stacks.append(current[::-1])
            current = [] if int(header.group(1), 16) == loop_thread else None
            continue
        frame = DUMP_FRAME.match(line)
        if frame and current is not None:
            path, number, name = frame.groups()
            current.append("%s:%s %s" % (Path(path).name, number, name))
    if current is not None:
        stacks.append(current[::-1])
    return stacks


def loop_stacks(path: Path) -> list[list[str]]:
    return stacks_of_the_loop(path.read_text(), threading.main_thread().ident or 0)


async def tick(seconds: float, dump, lateness: list[float]) -> None:
    "Sleep `TICK` again and again, and record how late each wake is."
    clock = anyio.current_time
    end = clock() + seconds
    while clock() < end:
        # From the wake asked for, so a dump is a tick late by `STALL`.
        faulthandler.dump_traceback_later(TICK + STALL, repeat=True, file=dump)
        asked = clock()
        await anyio.sleep(TICK)
        lateness.append(clock() - asked - TICK)
    faulthandler.cancel_dump_traceback_later()


def deliberate_stall() -> None:
    time.sleep(CALIBRATION)


async def calibrate() -> int:
    "Block the loop on purpose, and count the dumps that caught it."
    path = OUT / "calibration.txt"
    with path.open("w") as dump:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(tick, 0.5, dump, [])
            await anyio.sleep(0.2)
            deliberate_stall()
    caught = sum(1 for s in loop_stacks(path) if any(frame.endswith(" deliberate_stall") for frame in s))
    assert caught, "the probe missed a stall of %d ms: %s" % (CALIBRATION * 1000, path)
    return caught


def attach_over_a_socket(pymux: Pymux, offer: dict) -> tuple[socket.socket, object]:
    "A client's socket, and the server's connection for it."
    server_sock, client_sock = socket.socketpair()
    before = set(pymux.connections)
    pymux.serving.accept_connection(PosixSocketConnection(server_sock))
    (connection,) = set(pymux.connections) - before
    client_sock.sendall(
        encode_json({"cmd": "size", "data": [SIZE.rows, SIZE.columns]})
        + encode_json(
            {
                "cmd": "start-gui",
                "detach-others": False,
                "read-only": False,
                "color-depth": ColorDepth.DEPTH_24_BIT,
                "term": "xterm-kitty",
                "colorterm": "truecolor",
                "hostname": "",
                "environment": {},
                "ttyname": "/dev/pts/1",
                "pid": os.getpid(),
                "client-options": [],
                "pings": False,
                "paced": False,
                "forwards": False,
                "saves-files": True,
                "data": "",
                **offer,
            }
        )
    )
    return client_sock, connection


async def measure(name: str, offer: dict) -> dict:
    lateness: list[float] = []
    pymux = Pymux()
    workdir = Path(tempfile.mkdtemp())
    pymux.socket_name = str(workdir / "pymux.sock")
    dump_path = OUT / ("faulthandler-%s.txt" % name)
    with dump_path.open("w") as dump:
        async with pymux.running():
            client_sock, connection = attach_over_a_socket(pymux, offer)
            # `cat` is the client: it takes every byte, in a process of
            # its own, as fast as a terminal that keeps up.
            async with await anyio.open_process(
                ["cat"], stdin=client_sock.fileno(), stdout=subprocess.DEVNULL
            ) as reader:
                state = await once(lambda: connection.client_state, 10.0, "the server never made a client")
                graphics = state.connection.graphics
                graphics.kitty_supported = True
                graphics.detected = True
                graphics.cell_width, graphics.cell_height = CELL
                graphics.cell_size_known = True
                await once(lambda: state.session.arrangement.get_active_window(), 10.0, "no first window")
                state.session.arrangement.get_active_window().options.wayland_scale = "1"
                await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
                runtime = workdir / "run"
                runtime.mkdir(mode=0o700)
                mpv = shutil.which("mpv")
                assert mpv, "mpv is not on the PATH"
                async with await anyio.open_process(
                    [
                        mpv,
                        "--vo=wlshm",
                        "--no-terminal",
                        "--no-audio",
                        "--loop",
                        "av://lavfi:testsrc2=size=1600x900:rate=60",
                    ],
                    env=dict(os.environ, WAYLAND_DISPLAY=pymux.wayland.display, XDG_RUNTIME_DIR=str(runtime)),
                ) as player:
                    try:
                        await once(lambda: pymux.wayland.panes, 20.0, "mpv never opened a pane")
                        # The first frames open the codec and size the window.
                        await anyio.sleep(2.0)
                        before = state.connection.pipe_connection.bytes_out
                        await tick(SECONDS, dump, lateness)
                        sent = state.connection.pipe_connection.bytes_out - before
                        (toplevel,) = pymux.wayland.panes
                        drawn = pymux.wayland.frame(toplevel)
                        window_size = drawn[:2] if drawn is not None else None
                        compressed = state.connection.pipe_connection._compressor is not None
                    finally:
                        if player.returncode is None:
                            player.kill()
                client_sock.close()
                reader.kill()

    stacks = loop_stacks(dump_path)
    ordered = sorted(lateness)

    def at(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * 1000, 2)

    return {
        "compressed": compressed,
        "window_size": window_size,
        "megabytes_a_second": round(sent / SECONDS / 1e6, 1),
        "ticks": len(lateness),
        "late_ms": {
            "p50": at(0.50),
            "p99": at(0.99),
            "max": round(ordered[-1] * 1000, 2),
            "mean": round(statistics.fmean(lateness) * 1000, 2),
        },
        "ticks_late_over_ms": {str(ms): sum(1 for late in lateness if late * 1000 > ms) for ms in (10, 20, 40)},
        # One sample is `STALL` of a stall: a stall of 30 ms is three.
        "stall_samples": len(stacks),
        "where_it_stalls": [
            {"samples": count, "stack": list(stack)}
            for stack, count in Counter(tuple(s[-DEPTH:]) for s in stacks).most_common(25)
        ],
        "innermost_frames": dict(Counter(s[-1] for s in stacks if s).most_common(25)),
    }


async def measure_all() -> dict:
    report: dict = {
        "seconds": SECONDS,
        "calibration_samples": await calibrate(),
    }
    for name, offer in ROUTES.items():
        report[name] = await measure(name, offer)
    return report


def main() -> None:
    report = anyio.run(measure_all)
    (OUT / "stalls.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
