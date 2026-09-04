"""
The wire between a caller and a pymux server.

A pymux server listens on a unix socket. Client and server exchange
JSON objects, one for each message, and a NUL byte ends each one.

Running a command takes one connection. The server answers with the
output, the errors and the exit code, and then it closes. So every
call here opens a socket of its own, the same way the command line of
pymux does.
"""
import getpass
import glob
import json
import os
import shlex
import socket
import tempfile
from typing import Iterable, List, NamedTuple, Optional, Sequence, Union

__all__ = [
    "CommandError",
    "CommandResult",
    "Connection",
    "ServerNotRunning",
    "socket_paths",
    "quote",
]

#: The byte that ends one JSON message on the wire.
_END = b"\0"

#: How much to read from the socket at a time.
_CHUNK = 4096


class ServerNotRunning(OSError):
    "No pymux server answers on that socket."


class CommandError(RuntimeError):
    """
    A command came back with a non-zero exit code.

    `result` carries what the server said, so a caller that wants the
    output of a failed command can still read it.
    """

    def __init__(self, command: str, result: "CommandResult") -> None:
        self.command = command
        self.result = result
        message = result.stderr.strip() or "exit code %d" % (result.exit_code,)
        super().__init__("%s: %s" % (command, message))


class CommandResult(NamedTuple):
    "What one command sent back."

    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        "True when the command succeeded."
        return self.exit_code == 0

    def lines(self) -> List[str]:
        "The output, one entry for each line, without the empty last one."
        return self.stdout.splitlines()


def quote(argument: str) -> str:
    """
    Make one argument safe to put in a command string.

    The server splits a command with `shlex`, so an argument that holds
    a space or a quote needs quoting. A bare ";" separates two commands
    there, and quoting does not hide it: the server sees the token and
    splits anyway. Send such an argument with `send-keys -l` instead.
    """
    return shlex.quote(argument)


def _as_command(command: Union[str, Sequence[str]]) -> str:
    "One command string, from a string or from a list of arguments."
    if isinstance(command, str):
        return command
    return " ".join(quote(argument) for argument in command)


def socket_paths() -> List[str]:
    """
    Every pymux socket of this user that the default place holds.

    A server started with a socket path of its own is not in here. Name
    that path to reach it.
    """
    pattern = "%s/pymux.sock.%s.*" % (tempfile.gettempdir(), getpass.getuser())
    return sorted(path for path in glob.glob(pattern) if _is_socket(path))


def _is_socket(path: str) -> bool:
    import stat

    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


class Connection:
    """
    One pymux server, addressed by the path of its socket.

    This object holds no socket of its own. Each command opens one,
    because the server closes the connection when the command ends.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def __repr__(self) -> str:
        return "Connection(%r)" % (self.socket_path,)

    def is_alive(self) -> bool:
        "True when a server answers on this socket."
        try:
            self._connect().close()
        except OSError:
            return False
        return True

    def run(
        self,
        command: Union[str, Sequence[str]],
        pane_id: Optional[str] = None,
        check: bool = True,
    ) -> CommandResult:
        """
        Run one command on the server and read what it says.

        Give the command as a string, or as a list of arguments that
        this quotes for you. A list is the safe form.

        `pane_id` names the pane that the command reads as the current
        one, for a caller that has no attached client.

        `check` raises `CommandError` on a non-zero exit code. Pass
        False for a command that answers with its exit code, such as
        `has-session`.
        """
        text = _as_command(command)
        sock = self._connect()
        try:
            self._send(sock, {"cmd": "run-command", "data": text, "pane_id": pane_id})
            result = self._read(sock)
        finally:
            sock.close()

        if check and not result.ok:
            raise CommandError(text, result)
        return result

    # ------------------------------------------------------------------

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
        except OSError as error:
            sock.close()
            raise ServerNotRunning(
                "no pymux server on %s" % (self.socket_path,)
            ) from error
        sock.setblocking(True)
        return sock

    @staticmethod
    def _send(sock: socket.socket, packet: object) -> None:
        sock.sendall(json.dumps(packet).encode("utf-8") + _END)

    @staticmethod
    def _read(sock: socket.socket) -> CommandResult:
        "Read until the server closes, and gather what it said."
        out: List[str] = []
        err: List[str] = []
        exit_code = 0
        buffer = b""

        while True:
            try:
                data = sock.recv(_CHUNK)
            except OSError:
                break
            if not data:
                break

            buffer += data
            while _END in buffer:
                raw, buffer = buffer.split(_END, 1)
                if not raw:
                    continue
                packet = json.loads(raw.decode("utf-8"))
                kind = packet.get("cmd")
                if kind == "out":
                    out.append(packet["data"])
                elif kind == "err":
                    err.append(packet["data"])
                elif kind == "exit":
                    exit_code = packet["code"]

        return CommandResult("".join(out), "".join(err), exit_code)
