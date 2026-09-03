import asyncio
import json
from asyncio import create_task
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Dict,
    List,
    Optional,
    TextIO,
    cast,
)

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from .graphics import ClientGraphics
from .kitty import KittyVt100Parser
from .log import logger
from .pipes import BrokenPipeError

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux

__all__ = ["ServerConnection"]


class ServerConnection:
    """
    For each client that connects, we have one instance of this class.
    """

    def __init__(self, pymux: "Pymux", pipe_connection) -> None:
        self.pymux = pymux

        self.pipe_connection = pipe_connection

        self.size = Size(rows=20, columns=80)
        self._closed = False

        self._recv_buffer = b""
        self.client_state: Optional["ClientState"] = None

        # Kitty keyboard protocol support of the outer terminal. The
        # client sends "kitty-detect" right after querying its terminal;
        # the reply of that query passes through this connection.
        self._kitty_detection_pending = False
        self._kitty_supported = False

        # Kitty graphics protocol state of the outer terminal. The same
        # query answers this one: the reply of the graphics query
        # arrives before the device attributes reply that closes the
        # detection.
        self.graphics = ClientGraphics(
            self._write_output_raw, self._flush_output
        )

        # The client input is parsed by the application that reads from
        # the pipe input (see `_ClientInput`). Give that input a parser
        # that also understands the kitty keyboard protocol, and route
        # terminal replies to `_handle_kitty_reply`.
        self._pipeinput = _ClientInput(
            self._send_packet, kitty_reply_callback=self._handle_kitty_reply
        )

        create_task(self._start_reading())

    def _write_output_raw(self, data: str) -> None:
        "Write to the outer terminal, without escaping. (For graphics.)"
        if self.client_state is not None:
            self.client_state.output.write_raw(data)

    def _flush_output(self) -> None:
        if self.client_state is not None:
            self.client_state.output.flush()

    def _handle_kitty_reply(self, data: str) -> None:
        """
        A terminal reply arrived from the outer terminal. The replies of
        the keyboard flags query and of the graphics query say which
        protocols the terminal speaks. The device attributes reply comes
        last and closes the detection.
        """
        if not self._kitty_detection_pending:
            return

        if data.startswith("\x1b_"):
            # APC string sequence: the reply of the graphics query.
            self.graphics.handle_reply(data)
            return

        if data.startswith("\x1b[?") and data.endswith("u"):
            # Reply of the "CSI ? u" query: the terminal supports the
            # keyboard protocol.
            self._kitty_supported = True
            return

        # Device attributes reply: the fence of the detection. What did
        # not answer by now is not supported.
        self._kitty_detection_pending = False
        self._send_packet(
            {
                "cmd": "kitty-keyboard",
                "data": {"supported": self._kitty_supported},
            }
        )
        # Enable the flags of the focused pane on this client's terminal
        # as well. (Other clients are kept in sync through
        # `Pymux.sync_kitty_flags`, which only sends on change.)
        self._send_packet(
            {
                "cmd": "kitty-keyboard",
                "data": {"flags": self.pymux.get_focused_kitty_flags()},
            }
        )

    async def _start_reading(self) -> None:
        while True:
            try:
                data = await self.pipe_connection.read()
                self._process(data)
            except BrokenPipeError:
                self.detach_and_close()
                break

            except asyncio.CancelledError:
                raise

            except Exception:
                # The read loop must never die silently: log the
                # exception and keep the connection alive.
                logger.exception("Exception while processing client packet.")

    def _process(self, data) -> None:
        """
        Process packet received from client.
        """
        # Posix pipe returns bytes, win32 returns str. Normalize to str.
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8")
            except Exception:
                logger.warning("Received invalid UTF-8 from client. Ignoring.")
                return
        try:
            packet = json.loads(data)
        except ValueError:
            # So far, this never happened. But it would be good to have some
            # protection.
            logger.warning("Received invalid JSON from client. Ignoring.")
            return

        # Handle commands.
        if packet["cmd"] == "run-command":
            # Handle this in a task. The command handler can produce output
            # that has to be sent back to the client.
            create_task(self._run_command(packet))
            return

        # Handle stdin.
        elif packet["cmd"] == "in":
            self._pipeinput.send_text(packet["data"])

        # The client queried its terminal for kitty keyboard protocol
        # support. (The replies come back as input on this connection.)
        elif packet["cmd"] == "kitty-detect":
            self._kitty_detection_pending = True

        # Set size. (The client reports the size.)
        elif packet["cmd"] == "size":
            rows, columns = packet["data"]
            self.size = Size(rows=rows, columns=columns)
            self.pymux.invalidate()

        # Start GUI. (Create CommandLineInterface front-end for pymux.)
        elif packet["cmd"] == "start-gui":
            detach_other_clients = bool(packet["detach-others"])
            color_depth = ColorDepth(packet["color-depth"])
            term = packet["term"]

            if detach_other_clients:
                for c in self.pymux.connections:
                    c.detach_and_close()

            print("Create app...")
            self._create_app(color_depth=color_depth, term=term)

    def _send_packet(self, data: object) -> None:
        """
        Send packet to client.
        """
        if self._closed:
            return

        data = json.dumps(data)

        async def send() -> None:
            try:
                await self.pipe_connection.write(data)
            except BrokenPipeError:
                self.detach_and_close()

        create_task(send())

    async def _run_command(self, packet: Dict[str, Any]) -> None:
        """
        Execute a run command from the client.
        """
        create_temp_cli = self.client_state is None

        if create_temp_cli:
            # If this client doesn't have a CLI. Create a Fake CLI where the
            # window containing this pane, is the active one. (The CLI instance
            # will be removed right after the command handler ran, so it
            # doesn't hurt too much and makes the code easier.)
            pane_id = packet.get("pane_id")
            self._create_app(start=False)
            if pane_id is not None:
                with set_app(self.client_state.app):
                    self.pymux.arrangement.set_active_window_from_pane_id(
                        int(pane_id)
                    )

        pymux = self.pymux
        pymux.command_output = []
        pymux.command_error = []

        with set_app(self.client_state.app):
            try:
                pymux.handle_command(packet["data"])
            finally:
                # Send the output of the command back to the client, and
                # close the connection.
                output = pymux.command_output
                errors = pymux.command_error
                pymux.command_output = None
                pymux.command_error = None

                try:
                    if output:
                        await self._write_packet(
                            {"cmd": "out", "data": "\n".join(output) + "\n"}
                        )
                    if errors:
                        await self._write_packet(
                            {"cmd": "err", "data": "\n".join(errors) + "\n"}
                        )
                    await self._write_packet(
                        {"cmd": "exit", "code": 1 if errors else 0}
                    )
                except BrokenPipeError:
                    pass
                self._close_connection()

    async def _write_packet(self, data_obj: object) -> None:
        """
        Write a packet to the client. (This waits for the write to complete.)
        """
        if self._closed:
            return

        data = json.dumps(data_obj)

        try:
            await self.pipe_connection.write(data)
        except BrokenPipeError:
            self.detach_and_close()

    def _create_app(
        self,
        color_depth: ColorDepth = ColorDepth.DEPTH_8_BIT,
        term: str = "xterm",
        start: bool = True,
    ) -> None:
        """
        Create CommandLineInterface for this client.
        Called when the client wants to attach the UI to the server.

        :param start: Start the application. (`False` for the temporary CLI
            that handles `run-command` packets. That one is removed right
            after the command handler ran.)
        """
        output = Vt100_Output(
            cast(TextIO, _SocketStdout(self._send_packet)),
            lambda: self.size,
            term=term,
        )

        client_state = self.pymux.add_client(
            input=self._pipeinput,
            output=output,
            connection=self,
            color_depth=color_depth,
        )
        self.client_state = client_state

        if start:

            async def run() -> None:
                try:
                    await client_state.app.run_async()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Application crashed.")
                finally:
                    self._close_connection()

            create_task(run())

    def _close_connection(self) -> None:
        # This is important. If we would forget this, the server will
        # render CLI output for clients that aren't connected anymore.
        if self._closed:
            return
        # Remove the images that this client put on its terminal.
        try:
            self.graphics.reset()
        except Exception:
            logger.exception("Removing the graphics of the client failed.")

        # Try to exit the application if it's still running.
        if self.client_state is not None:
            try:
                if not self.client_state.app.is_done:
                    self.client_state.app.exit()
            except Exception:
                pass
        self.pymux.remove_client(self)
        self.client_state = None
        self._closed = True

        # Close input pipe and remove connection from eventloop.
        self._pipeinput.close()
        try:
            self.pipe_connection.close()
        except Exception:
            pass

    def suspend_client_to_background(self) -> None:
        """
        Ask the client to suspend itself. (Like, when Ctrl-Z is pressed.)
        """
        self._send_packet({"cmd": "suspend"})

    def detach_and_close(self) -> None:
        # Remove from Pymux.
        self._close_connection()


class _SocketStdout:
    """
    Stdout-like object that writes everything through the unix socket to the
    client.
    """

    def __init__(self, send_packet: Callable) -> None:
        self.send_packet = send_packet
        self._buffer: List[str] = []

    def write(self, data: str) -> int:
        self._buffer.append(data)
        return len(data)

    def flush(self) -> None:
        data = {"cmd": "out", "data": "".join(self._buffer)}
        self.send_packet(data)
        self._buffer = []

    def isatty(self) -> bool:
        return True


class _ClientInput:
    """
    Input class that can be given to the CommandLineInterface.
    We only need this for turning the client into raw_mode/cooked_mode.
    """

    def __init__(self, send_packet: Callable, kitty_reply_callback=None) -> None:
        self.send_packet = send_packet
        # Keep a reference to the context manager for the whole lifetime of
        # this object. `create_pipe_input()` returns a generator context
        # manager; when it's garbage collected, the pipe is closed.
        self._input_cm = create_pipe_input()
        self._input = self._input_cm.__enter__()

        # Replace the parser of the pipe input with one that also
        # understands the key encoding of the kitty keyboard protocol,
        # and routes terminal replies (keyboard flags query, device
        # attributes) to the given callback.
        self._input.vt100_parser = KittyVt100Parser(
            lambda key_press: self._input._buffer.append(key_press),
            reply_callback=kitty_reply_callback,
        )

    def close(self) -> None:
        "Close the input pipe. (Idempotent.)"
        if self._input is not None:
            try:
                self._input_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._input = None

    def typeahead_hash(self) -> str:
        """
        The typeahead hash must keep working after the pipe is closed:
        prompt_toolkit stores unprocessed input as typeahead when the
        application exits, which can happen after the connection was
        closed.
        """
        if self._input is not None:
            return self._input.typeahead_hash()
        return "closed-pipe-input-%i" % id(self)

    # Implement raw/cooked mode by sending this to the attached client.

    def raw_mode(self) -> ContextManager[None]:
        return self._create_context_manager("raw")

    def cooked_mode(self) -> ContextManager[None]:
        return self._create_context_manager("cooked")

    def _create_context_manager(self, mode: str) -> ContextManager[None]:
        "Create a context manager that sends 'mode' commands to the client."

        class mode_context_manager:
            def __enter__(*a: object) -> None:
                self.send_packet({"cmd": "mode", "data": mode})

            def __exit__(*a: object) -> None:
                self.send_packet({"cmd": "mode", "data": "restore"})

        return mode_context_manager()

    def __getattr__(self, name: str) -> object:
        return getattr(self._input, name)
