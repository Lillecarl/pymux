"""
What crosses the wire between server and client, in bytes.

Scratch measurement for Lillecarl/pymux#258. TEMPORARY: not a gate,
judges nothing, prints a report. It drives a real `ServerConnection`
over `pipes.memory` -- the transport of `pymux integrated` -- and
records every packet the server sends, encoded the way the socket route
encodes it (plus the one zero byte the socket framing adds).

Four scenarios: attaching and idling with the default status line (so
the clock ticks), typing at an echoing pane, a pane dumping lines as
fast as it can, and a pane repainting the whole screen over and over.

Run with:

    nix build --file . checks.pymux-wire.run
    less result/log
"""

import asyncio
import json
import statistics
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# And pymux itself, which the sandbox copies beside `tests` rather than
# installing. A script's own directory is what Python puts on the path,
# not the directory above it.
sys.path.insert(1, str(Path(__file__).parent.parent))

from session import over_a_connection  # noqa: E402
from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402

from pymux.main import Pymux  # noqa: E402

#: The client's terminal.
SIZE = Size(rows=24, columns=80)

#: How long one wait may take before the run gives up.
PATIENCE = 15.0

#: Markers that cannot appear in the chrome (see `measure_latency.py`).
MARKERS = "~!@#$^&"

#: The longest `status-interval` the option takes: the auto refresh must
#: not tick inside a scenario that measures something else.
NO_REFRESH = 60

#: A pane that says it is up and waits.
QUIET_CHILD = """
import sys
sys.stdout.write("READY")
sys.stdout.flush()
sys.stdin.read()
"""

#: A pane that echoes every byte it reads.
ECHO_CHILD = r"""
import os, tty
tty.setraw(0)
while True:
    data = os.read(0, 1)
    if not data:
        break
    os.write(1, b"\r" + data)
"""

#: A pane that dumps lines as fast as it can, then waits.
BURST_CHILD = """
import sys
for i in range(300):
    sys.stdout.write("line %4d %s\\n" % (i, "x" * 60))
sys.stdout.write("DONE\\n")
sys.stdout.flush()
sys.stdin.read()
"""

#: A pane that repaints the whole screen eight times. It waits for a
#: key first, so the churn starts after the client attached: an attach
#: takes longer than eight repaints, and a marker that scrolled away
#: before the wait started would never arrive.
CHURN_CHILD = """
import sys, time, tty
tty.setraw(0)
sys.stdin.read(1)
for i in range(8):
    sys.stdout.write("\\x1b[2J\\x1b[H")
    for r in range(24):
        sys.stdout.write("row %02d %s\\n" % (r, chr(65 + (i + r) % 26) * 60))
    sys.stdout.write("MARK%d\\n" % i)
    sys.stdout.flush()
    time.sleep(0.25)
sys.stdin.read()
"""


class Recorder:
    """
    Every packet the server sends, and a way to wait for one.

    `over_a_connection` calls this with each packet off the queue. The
    raw bytes are kept, so the report can compress exactly what the
    socket route would carry (plus its one zero byte).
    """

    def __init__(self, loop):
        self.loop = loop
        self.packets: list = []
        self.waiting: dict = {}

    def wait_for(self, marker: str):
        future = self.loop.create_future()
        self.waiting.setdefault(marker, []).append(future)
        return future

    def __call__(self, packet) -> None:
        if isinstance(packet, (bytes, bytearray)):
            raw = bytes(packet)
        else:
            raw = str(packet).encode("utf-8")
        self.packets.append(raw)

        try:
            given = json.loads(raw.decode("utf-8"))
        except ValueError:
            return
        if given.get("cmd") != "out":
            return
        data = given.get("data", "")
        for marker, futures in list(self.waiting.items()):
            if marker in data:
                for future in futures:
                    if not future.done():
                        future.set_result(True)
                del self.waiting[marker]


async def wait_for(recorder, marker):
    await asyncio.wait_for(recorder.wait_for(marker), PATIENCE)


def wire_len(raw: bytes) -> int:
    "What this packet costs on the socket route: the encoding plus NUL."
    return len(raw) + 1


def scenario_report(name, packets) -> dict:
    "Print what one scenario sent, and return its out-packet wire bytes."
    by_cmd: dict = {}
    for raw in packets:
        try:
            cmd = json.loads(raw.decode("utf-8")).get("cmd", "?")
        except ValueError:
            cmd = "?"
        by_cmd.setdefault(cmd, []).append(raw)

    total = sum(wire_len(raw) for raw in packets)
    print("\n--- %s: %d packets, %d wire bytes ---" % (name, len(packets), total))
    for cmd in sorted(by_cmd):
        raws = by_cmd[cmd]
        print(
            "  %-14s %5d packets %9d bytes"
            % (cmd, len(raws), sum(wire_len(r) for r in raws))
        )

    out = [wire_len(r) for r in by_cmd.get("out", [])]
    if out:
        print(
            "  out-packet wire size: min %d, median %d, mean %.0f, max %d"
            % (min(out), statistics.median(out), statistics.fmean(out), max(out))
        )
        # The framing share: JSON around the frame versus the frame.
        payload = 0
        for raw in by_cmd["out"]:
            try:
                payload += len(json.loads(raw.decode("utf-8")).get("data", ""))
            except ValueError:
                pass
        print(
            "  frame text %d bytes of %d wire bytes (%.0f%% framing)"
            % (payload, sum(out), 100.0 * (sum(out) - payload) / sum(out))
        )
        # How much of the wire is JSON escaping an escape: `\u001b` is
        # six bytes for a one-byte character, and frames are made of
        # escape sequences.
        escaped = sum(r.count(b"\\u001b") for r in by_cmd["out"])
        print(
            "  %d escaped ESCs cost %d bytes (%.0f%% of these wire bytes)"
            % (escaped, escaped * 5, 100.0 * escaped * 5 / sum(out))
        )
    return {"out_wire": [r for r in by_cmd.get("out", [])]}


def compression_report(all_out) -> None:
    "What zlib does to the out packets, per packet and as a stream."
    import msgpack

    wire = b"".join(raw + b"\0" for raw in all_out)
    print(
        "\n--- compression over %d out packets, %d wire bytes ---"
        % (len(all_out), len(wire))
    )

    for level in (1, 6):
        per_packet = sum(len(zlib.compress(raw + b"\0", level)) for raw in all_out)
        stream = len(zlib.compress(wire, level))
        print(
            "  zlib level %d: per-packet %d bytes (%.1f%%), stream %d bytes (%.1f%%)"
            % (
                level,
                per_packet,
                100.0 * per_packet / len(wire),
                stream,
                100.0 * stream / len(wire),
            )
        )

    # Where the per-packet cost goes: the smallest and largest tenth.
    sizes = sorted(
        (len(zlib.compress(raw + b"\0", 1)) / (len(raw) + 1)) for raw in all_out
    )
    if sizes:
        print(
            "  per-packet level-1 ratio: median %.1f%%, worst tenth %.1f%%"
            % (
                100.0 * sizes[len(sizes) // 2],
                100.0 * statistics.fmean(sizes[-max(1, len(sizes) // 10) :]),
            )
        )

    # The same packets as msgpack, then zlib on top of that.
    packed = [
        msgpack.packb(json.loads(raw.decode("utf-8"))) for raw in all_out
    ]
    mp_wire = sum(len(p) for p in packed)
    print(
        "\n--- the same %d packets as msgpack: %d bytes (%.1f%% of JSON) ---"
        % (len(packed), mp_wire, 100.0 * mp_wire / len(wire))
    )
    for level in (1, 6):
        per_packet = sum(len(zlib.compress(p, level)) for p in packed)
        stream = len(zlib.compress(b"".join(packed), level))
        print(
            "  msgpack + zlib level %d: per-packet %d bytes (%.1f%% of JSON), "
            "stream %d bytes (%.1f%% of JSON)"
            % (
                level,
                per_packet,
                100.0 * per_packet / len(wire),
                stream,
                100.0 * stream / len(wire),
            )
        )

    # Where the crossover is: per packet size, does zlib level 1 win?
    print("\n--- per-packet msgpack size vs zlib level 1 winner ---")
    buckets: dict = {}
    for p in packed:
        won = len(zlib.compress(p, 1)) < len(p)
        buckets.setdefault(len(p) // 100 * 100, [0, 0])[won] += 1
    for floor in sorted(buckets):
        lost, won = buckets[floor]
        print(
            "  %4d-%4d bytes: zlib wins %d, raw wins %d"
            % (floor, floor + 99, won, lost)
        )


async def scenario_idle(tmp, recorder) -> list:
    "Attach with the defaults and idle past two clock ticks."
    child = tmp / "quiet.py"
    child.write_text(QUIET_CHILD)
    pymux = Pymux(startup_command="%s %s" % (sys.executable, child))
    before = len(recorder.packets)
    with over_a_connection(pymux=pymux, read_a_packet=recorder) as session:
        state, _size = await session.attach("idle", SIZE)
        with set_app(state.app):
            await wait_for(recorder, "READY")
            await asyncio.sleep(10)
            await session.detach(state)
    return recorder.packets[before:]


async def scenario_typing(tmp, recorder) -> list:
    "Thirty keystrokes at an echoing pane, clock and refresh off."
    child = tmp / "echo.py"
    child.write_text(ECHO_CHILD)
    pymux = Pymux(startup_command="%s %s" % (sys.executable, child))
    pymux.status_interval = NO_REFRESH
    before = len(recorder.packets)
    with over_a_connection(pymux=pymux, read_a_packet=recorder) as session:
        state, _size = await session.attach("typing", SIZE)
        with set_app(state.app):
            pymux.handle_command("set-option status-right ''")
            for number in range(30):
                marker = MARKERS[number % len(MARKERS)]
                session.typed(state, marker)
                await wait_for(recorder, marker)
                await asyncio.sleep(0.03)
            await session.detach(state)
    return recorder.packets[before:]


async def scenario_burst(tmp, recorder) -> list:
    "A pane dumping 300 lines as fast as it can."
    child = tmp / "burst.py"
    child.write_text(BURST_CHILD)
    pymux = Pymux(startup_command="%s %s" % (sys.executable, child))
    pymux.status_interval = NO_REFRESH
    before = len(recorder.packets)
    with over_a_connection(pymux=pymux, read_a_packet=recorder) as session:
        state, _size = await session.attach("burst", SIZE)
        with set_app(state.app):
            pymux.handle_command("set-option status-right ''")
            await wait_for(recorder, "DONE")
            await asyncio.sleep(1.0)
            await session.detach(state)
    return recorder.packets[before:]


async def scenario_churn(tmp, recorder) -> list:
    "A pane repainting the whole screen eight times."
    child = tmp / "churn.py"
    child.write_text(CHURN_CHILD)
    pymux = Pymux(startup_command="%s %s" % (sys.executable, child))
    pymux.status_interval = NO_REFRESH
    before = len(recorder.packets)
    with over_a_connection(pymux=pymux, read_a_packet=recorder) as session:
        state, _size = await session.attach("churn", SIZE)
        with set_app(state.app):
            pymux.handle_command("set-option status-right ''")
            # "~" passes through to the pane: the typing scenario proves
            # it, where a letter might be a client binding instead.
            session.typed(state, "~")
            # One gate, not eight: the first paint writes the whole
            # "MARK0" line, but later frames carry only the digit that
            # changed, never the whole marker. Eight repaints take two
            # seconds; four covers them with room.
            await wait_for(recorder, "MARK0")
            await asyncio.sleep(4.0)
            await session.detach(state)
    return recorder.packets[before:]


async def measure() -> None:
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)

        recorder = Recorder(loop)
        idle = await scenario_idle(tmp, recorder)
        recorder2 = Recorder(loop)
        typing = await scenario_typing(tmp, recorder2)
        recorder3 = Recorder(loop)
        burst = await scenario_burst(tmp, recorder3)
        recorder4 = Recorder(loop)
        churn = await scenario_churn(tmp, recorder4)

    print("wire sizes in %ds" % int(time.monotonic() - started))
    kept = []
    kept += scenario_report("attach and idle 10s (clock on)", idle)["out_wire"]
    kept += scenario_report("30 keystrokes at an echo pane", typing)["out_wire"]
    kept += scenario_report("a pane dumping 300 lines", burst)["out_wire"]
    kept += scenario_report("eight full-screen repaints", churn)["out_wire"]
    compression_report(kept)


def main() -> int:
    asyncio.run(measure())
    return 0


if __name__ == "__main__":
    sys.exit(main())
