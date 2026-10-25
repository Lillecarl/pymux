"""
The wire between a caller and a pymux server.

A pymux server listens on a unix socket. Client and server exchange
JSON objects, one for each message, and a NUL byte ends each one.

Running a command takes one connection. The server answers with the
output, the errors and the exit code, and then it closes. So every
call here opens a socket of its own, the same way the command line of
pymux does.

`command_packet` is the bytes to send and `Reply` reads what comes back.
Neither does I/O, so a test reaches them with no socket, and
`Connection` only moves the bytes.
"""

import json
import shlex
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Union

import anyio
import anyio.abc

from .agentic import caller_fields
from .protocol import END, READ_SIZE, Field, Framer, Packet

__all__ = [
    "CommandError",
    "CommandResult",
    "Connection",
    "Reply",
    "ServerNotRunning",
    "command_packet",
    "quote",
]


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


def as_command(command: Union[str, Sequence[str]]) -> str:
    "One command string, from a string or from a list of arguments."
    if isinstance(command, str):
        return command
    return " ".join(quote(argument) for argument in command)


def command_packet(text: str, pane_id: Optional[str] = None, caller: Optional[Dict[str, object]] = None) -> bytes:
    """
    The bytes that ask the server to run one command.

    `caller` is the fields `agentic.caller_fields` makes: who sent the
    command, and from which directory.
    """
    packet = {Field.CMD: Packet.RUN_COMMAND, Field.DATA: text, Field.PANE_ID: pane_id, **(caller or {})}
    return json.dumps(packet).encode("utf-8") + END


class Reply:
    """
    What the server says to one command, fed in as it arrives.

    The server writes the output and the errors and then the exit code,
    and closes. So a caller feeds every read and asks for `result()` at
    the close.
    """

    def __init__(self) -> None:
        self._framer = Framer()
        self._out: List[str] = []
        self._err: List[str] = []
        self._exit_code = 0

    def feed(self, data: bytes) -> None:
        for raw in self._framer.feed(data):
            if not raw:
                continue
            packet = json.loads(raw.decode("utf-8"))
            kind = packet.get(Field.CMD)
            if kind == Packet.OUT:
                self._out.append(packet[Field.DATA])
            elif kind == Packet.ERR:
                self._err.append(packet[Field.DATA])
            elif kind == Packet.EXIT:
                self._exit_code = packet[Field.CODE]

    def result(self) -> CommandResult:
        return CommandResult("".join(self._out), "".join(self._err), self._exit_code)


def checked(text: str, result: CommandResult, check: bool) -> CommandResult:
    "`result`, or `CommandError` when `check` asks for a zero exit code."
    if check and not result.ok:
        raise CommandError(text, result)
    return result


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

    async def is_alive(self) -> bool:
        "True when a server answers on this socket."
        try:
            stream = await self._connect()
        except ServerNotRunning:
            return False
        await stream.aclose()
        return True

    async def run(
        self,
        command: Union[str, Sequence[str]],
        pane_id: Optional[str] = None,
        check: bool = True,
        environment: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
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

        `environment` and `cwd` say who sends the command, as the
        command line of pymux says it: a job starts in that directory
        with that environment, and the agent session id in it stamps
        the job. They default to this process's own.
        """
        text = as_command(command)
        reply = Reply()
        async with await self._connect() as stream:
            await stream.send(command_packet(text, pane_id, caller_fields(environment, cwd)))
            while True:
                try:
                    data = await stream.receive(READ_SIZE)
                except anyio.EndOfStream, anyio.ClosedResourceError, OSError:
                    break
                reply.feed(data)
        return checked(text, reply.result(), check)

    async def _connect(self) -> anyio.abc.SocketStream:
        try:
            return await anyio.connect_unix(self.socket_path)
        except OSError as error:
            raise ServerNotRunning("no pymux server on %s" % (self.socket_path,)) from error
