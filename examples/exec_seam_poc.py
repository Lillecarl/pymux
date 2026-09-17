#!/usr/bin/env python3
"""
A standalone proof for the hot-upgrade exec seam. Lillecarl/pymux#408.

An "old server" forks pane children on ptys and binds a unix socket,
then execve's THIS script back as the "new server", same process, same
PID. Nothing of pymux is imported: the claims are POSIX claims, and a
live process proves each one. The old server reads nothing after the
fork -- that silence is the freeze window -- so every line the pane
writes has to cross it in the kernel pty queue.

The claims, one PASS/FAIL line each:

1. the inheritable-fd pass carries pty master, the parent's slave
   copy and the listener across the execve, fd numbers intact;
2. the new server verifies each fd (fstat, ttyname, minor numbers)
   instead of trusting the manifest;
3. the pane child never notices: still alive after the exec, no
   SIGHUP, and its numbered lines arrive contiguously -- no loss, no
   repeat, both before and after the swap;
4. the listener still accepts a client;
5. the reaper gap is real: a child that dies inside the window is a
   zombie until the new server reaps it, and the new server can;

The control (run with --control) execs WITHOUT the pass and expects
the master gone (EBADF), which is why the pass exists at all: PEP 446
marks every fd non-inheritable by default.

    python3 exec_seam_poc.py             # the five claims
    python3 exec_seam_poc.py --control   # the necessity
"""

import fcntl
import json
import os
import select
import signal
import socket
import stat
import struct
import sys
import tempfile
import termios
import time

# _IOR('t', 0x30, unsigned int): asks a pty master for its index, the
# N of /dev/pts/N. Not exported by the termios module.
TIOCGPTN = 0x80045430

PANE_PROGRAM = """
import time
for i in range(1, 41):
    print("line %d" % i, flush=True)
    time.sleep(0.05)
"""

READ_SECONDS = 2.5


def fork_pane(program):
    "A pane child on its own session, controlling tty on the slave."
    master, slave = os.openpty()
    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        os.execv(sys.executable, [sys.executable, "-c", program])
    return master, slave, pid


def old_server(control):
    "The first life of the server process: fork panes, bind, exec."
    room = tempfile.mkdtemp(prefix="poc-exec-seam-")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_path = os.path.join(room, "poc.sock")
    listener.bind(socket_path)
    listener.listen()

    master, parent_slave, pane_pid = fork_pane(PANE_PROGRAM)
    zombie_pid = os.fork()
    if zombie_pid == 0:
        time.sleep(0.35)
        os._exit(42)  # The reaper that would take this is dying.

    if not control:
        # The pass: a deliberate, listable act. Without it every fd
        # here dies at the execve, and the control shows it.
        for fd in (master, parent_slave, listener.fileno()):
            os.set_inheritable(fd, True)

    manifest = {
        "ptys": [{"master": master, "slave": parent_slave, "pid": pane_pid}],
        "zombie_pid": zombie_pid,
        "listener": listener.fileno(),
        "socket_path": socket_path,
    }
    path = os.path.join(room, "state.json")
    with open(path, "w") as f:
        json.dump(manifest, f)

    print("old server: execve, same pid, fds %s" % sorted(
        [master, parent_slave, listener.fileno()]), flush=True)
    os.execve(
        sys.executable,
        [sys.executable, os.path.abspath(__file__),
         "--new-server", path, "--control" if control else ""],
        os.environ,
    )


def verify_pty(master, slave):
    "The fds still are the pty they were: same device, right kinds."
    master_stat = os.fstat(master)
    slave_stat = os.fstat(slave)
    assert stat.S_ISCHR(master_stat.st_mode), "master is not a char device"
    assert stat.S_ISCHR(slave_stat.st_mode), "slave is not a char device"
    # A master reports itself as /dev/ptmx (5:2) -- every master looks
    # alike. TIOCGPTN holds the real index, the slave's rdev minor
    # carries the same one, and the two ends agreeing is what "same
    # pty" means here: checked in the kernel, not trusted from the
    # manifest.
    answer = bytearray(4)
    fcntl.ioctl(master, TIOCGPTN, answer)
    index = struct.unpack("I", answer)[0]
    assert os.minor(slave_stat.st_rdev) == index, (
        "master and slave are not the same pty")
    assert os.ttyname(slave).startswith("/dev/"), "slave has no tty name"


def read_the_lines(master, quiet_seconds=0.4):
    "Read numbered lines off the inherited master until it goes quiet."
    numbers = []
    deadline = time.monotonic() + READ_SECONDS
    buffer = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], quiet_seconds)
        if not ready:
            break
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.startswith(b"line "):
                numbers.append(int(line.split()[1]))
    return numbers


def new_server(manifest_path, control):
    "The second life: verify, re-bind, resume, reap."
    with open(manifest_path) as f:
        manifest = json.load(f)
    pane = manifest["ptys"][0]
    verdicts = []

    def verdict(name, ok, detail=""):
        verdicts.append(ok)
        print("PASS  %s %s" % (name, detail) if ok
              else "FAIL  %s %s" % (name, detail), flush=True)

    if control:
        try:
            os.fstat(pane["master"])
            verdict("control", False, "master survived without the pass?")
        except OSError as error:
            verdict("control", True, "master gone at exec: %s" % error)
        _cleanup(pane, manifest)
        sys.exit(0 if all(verdicts) else 1)

    # 1 + 2. The pass carried the fds, and they verify as the same
    # pty -- not trusted from the manifest, checked in the kernel.
    try:
        verify_pty(pane["master"], pane["slave"])
        verdict("fds-carried-and-verified", True,
                "master=%d slave=%d pid=%d"
                % (pane["master"], pane["slave"], pane["pid"]))
    except (OSError, AssertionError) as error:
        verdict("fds-carried-and-verified", False, str(error))

    # 3. The pane never noticed. Alive after the exec, and the lines
    # it wrote on both sides of the swap arrive contiguous: the
    # freeze window lost nothing and repeated nothing.
    numbers = read_the_lines(pane["master"])
    contiguous = numbers == list(range(1, len(numbers) + 1)) and len(numbers) >= 10
    try:
        os.kill(pane["pid"], 0)
        alive = True
    except ProcessLookupError:
        alive = False
    verdict("pane-never-noticed", contiguous and alive,
            "%d lines contiguous, child alive: %s" % (len(numbers), alive))

    # 4. The listener still accepts.
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM,
                                 fileno=manifest["listener"])
        assert listener.getsockname() == manifest["socket_path"]
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(manifest["socket_path"])
        connection, _ = listener.accept()
        connection.send(b"still-here")
        assert client.recv(64) == b"still-here"
        client.close()
        connection.close()
        verdict("listener-still-accepts", True)
    except (OSError, AssertionError) as error:
        verdict("listener-still-accepts", False, str(error))

    # 5. The reaper gap. The zombie died inside the window and nobody
    # of the old server took it; this process is its parent and can.
    reaped = os.waitpid(manifest["zombie_pid"], os.WNOHANG)
    verdict("reaper-gap-closed",
            reaped[0] == manifest["zombie_pid"]
            and os.waitstatus_to_exitcode(reaped[1]) == 42,
            "reaped exit %s"
            % os.waitstatus_to_exitcode(reaped[1]) if reaped[1] else "")

    _cleanup(pane, manifest)
    sys.exit(0 if all(verdicts) else 1)


def _cleanup(pane, manifest):
    "Take the surviving child down and leave no socket file behind."
    try:
        os.kill(pane["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    for fd in (pane["master"], pane["slave"]):
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.waitpid(pane["pid"], 0)
    except ChildProcessError:
        pass
    try:
        os.unlink(manifest["socket_path"])
    except OSError:
        pass


if __name__ == "__main__":
    if "--new-server" in sys.argv:
        path = sys.argv[sys.argv.index("--new-server") + 1]
        control = "--control" in sys.argv
        new_server(path, control)
    else:
        old_server("--control" in sys.argv)
