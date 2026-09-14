import json
import os
import socket
import sys
from ctypes import byref, windll
from ctypes.wintypes import DWORD

import anyio

from prompt_toolkit.input.win32 import Win32Input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.win32 import Win32Output
from prompt_toolkit.win32_types import STD_OUTPUT_HANDLE

from ..config import client_options_in, find_config
from ..log import logger
from ..pipes.win32_client import PipeClient
from .base import Client

__all__ = [
    "WindowsClient",
    "list_clients",
]

# See: https://msdn.microsoft.com/pl-pl/library/windows/desktop/ms686033(v=vs.85).aspx
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


class WindowsClient(Client):
    def __init__(self, pipe_name):
        self._input = Win32Input()
        self._hconsole = windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        self._data_buffer = b""

        self.pipe = PipeClient(pipe_name)

        #: What this client leaves with. Lillecarl/pymux#332.
        self.exit_code = 0

        #: The scope the writes of this client run in. `_attach` opens
        #: it, and `_send_packet` is called from a keyboard callback,
        #: which is not a coroutine. Lillecarl/pymux#87.
        self._tasks: "anyio.abc.TaskGroup" | None = None

    def attach(
        self, detach_other_clients: bool = False, color_depth=ColorDepth.DEPTH_8_BIT
    ):
        anyio.run(self._attach, detach_other_clients, color_depth)

    async def _attach(self, detach_other_clients: bool, color_depth) -> None:
        async with anyio.create_task_group() as tasks:
            self._tasks = tasks

            self._send_size()
            self._send_packet(
                {
                    "cmd": "start-gui",
                    "detach-others": detach_other_clients,
                    # Lillecarl/pymux#347, as `client/terminal.py` says.
                    "hang-up-others": self.hang_up_others,
                    "color-depth": color_depth,
                    "term": os.environ.get("TERM", ""),
                    # Lillecarl/pymux#287, as `client/terminal.py` says.
                    "hostname": socket.gethostname(),
                    # Lillecarl/pymux#271, and the same file says why
                    # the client sends all of it.
                    "environment": dict(os.environ),
                    # Windows has no tty path, so a client here is
                    # always named by its process. Lillecarl/pymux#335.
                    "ttyname": "",
                    "pid": os.getpid(),
                    # Lillecarl/pymux#223, as `client/terminal.py` says.
                    # The same two sources in the same order.
                    "client-options": (
                        client_options_in(self.config_file or find_config())
                        + ([("name", self.chosen_name)] if self.chosen_name else [])
                    ),
                    "data": "",
                }
            )

            try:
                with self._input.attach(self._input_ready):
                    # Run as long as we have a connection with the server.
                    await self._start_reader()
            finally:
                self._tasks = None
                tasks.cancel_scope.cancel()

    async def _start_reader(self):
        """
        Read messages from the Win32 pipe server and handle them.
        """
        while True:
            message = await self.pipe.read_message()
            self._process(message)

    def _process(self, data_buffer):
        """
        Handle incoming packet from server.
        """
        packet = json.loads(data_buffer)

        if packet["cmd"] == "out":
            # Call os.write manually. In Python2.6, sys.stdout.write doesn't use UTF-8.
            original_mode = DWORD(0)
            windll.kernel32.GetConsoleMode(self._hconsole, byref(original_mode))

            windll.kernel32.SetConsoleMode(
                self._hconsole,
                DWORD(ENABLE_PROCESSED_INPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING),
            )

            try:
                os.write(sys.stdout.fileno(), packet["data"].encode("utf-8"))
            finally:
                windll.kernel32.SetConsoleMode(self._hconsole, original_mode)

        elif packet["cmd"] == "exit":
            # What this client leaves with. Lillecarl/pymux#332, as
            # `client/terminal.py` says.
            self.exit_code = packet["code"]
            # Windows has no SIGHUP, so this is remembered and the
            # hangup is what does nothing. Lillecarl/pymux#347.
            if packet.get("hang-up"):
                self.hang_up_asked = True

        elif packet["cmd"] == "suspend":
            # Suspend client process to background.
            pass

        elif packet["cmd"] == "mode":
            pass

            # # Set terminal to raw/cooked.
            # action = packet['data']

            # if action == 'raw':
            #     cm = raw_mode(sys.stdin.fileno())
            #     cm.__enter__()
            #     self._mode_context_managers.append(cm)

            # elif action == 'cooked':
            #     cm = cooked_mode(sys.stdin.fileno())
            #     cm.__enter__()
            #     self._mode_context_managers.append(cm)

            # elif action == 'restore' and self._mode_context_managers:
            #     cm = self._mode_context_managers.pop()
            #     cm.__exit__()

    def _input_ready(self):
        keys = self._input.read_keys()
        if keys:
            self._send_packet(
                {
                    "cmd": "in",
                    "data": "".join(key_press.data for key_press in keys),
                }
            )

    def _send_packet(self, data):
        "Send to server."
        data = json.dumps(data)
        if self._tasks is None:
            return  # Not attached, so there is nothing to send to.
        self._tasks.start_soon(self._write_packet, data)

    async def _write_packet(self, data: str) -> None:
        """
        Write one packet, and say what went wrong.

        The return of the write used to be dropped, so a write that
        failed failed in silence. Lillecarl/pymux#87.
        """
        try:
            await self.pipe.write_message(data)
        except Exception:
            logger.exception("Sending a packet to the server failed.")

    def _send_size(self):
        "Report terminal size to server."
        output = Win32Output(sys.stdout)
        rows, cols = output.get_size()

        self._send_packet({"cmd": "size", "data": [rows, cols]})


def list_clients():
    return []
