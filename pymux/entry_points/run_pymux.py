#!/usr/bin/env python
"""
pymux: Pure Python terminal multiplexer.

Usage:
    pymux [options] [standalone|integrated|start-server|attach|list-sessions] [<command> ...]

Running pymux without arguments starts a server (daemonized) and attaches
a client to it. Any other command (e.g. ``split-window``) is sent to a
running server, like tmux does.

Modes:
    standalone     : Run as a standalone process. (for debugging, detaching
                     is not possible.)
    integrated     : Run a server and one client in this process. They talk
                     through queues, not through a socket, so the client
                     reaches the server this command started and nothing
                     else. With -S, the server also listens on that socket
                     for commands. One process holds both halves, so Ctrl-Z
                     suspends the server too, and detaching ends the whole
                     thing.
    start-server   : Run a server daemon that can be attached later on.
    attach         : Attach to a running session.
    list-sessions  : List all running sessions. ('ls' works as well.)

Options:
    -S SOCKET      : Unix socket path. A number is accepted as well; the
                     socket will be created in the temp directory.
    -f FILE        : Path to configuration file. By default: '~/.pymux.conf'.
    -d             : Detach all other clients, when attaching.
    --log FILE     : Logfile.
    --truecolor    : Render true color (24 bit) instead of 256 colors.
                     (Each client can set this separately.)
    --ansicolor    : Use only the 16 ANSI colors.
    --version      : Print version and exit.
"""
import argparse
import getpass
import logging
import os
import shlex
import socket
import sys
import tempfile
import time
from typing import Dict, List, Set, Tuple

from prompt_toolkit.output import ColorDepth

from pymux import __version__
from pymux.client import create_client, list_clients
from pymux.main import Pymux
from pymux.utils import daemonize

__all__ = ["run"]

MODES = (
    "standalone",
    "integrated",
    "start-server",
    "attach",
    "list-sessions",
    "ls",
)

#: The modes that take the command of the first pane after the mode
#: word, rather than a pymux command for a running server.
MODES_WITH_A_FIRST_PANE = ("standalone", "integrated")


def filename_var() -> str | None:
    """
    Return the configuration file name for the current invocation. (This is
    stored as a module global, because the daemonized server needs it after
    the fork.)
    """
    return _current_filename


_current_filename: str | None = None


def _add_options(parser: argparse.ArgumentParser, suppress_defaults: bool) -> None:
    """
    Add the pymux options to the parser.

    :param suppress_defaults: When `True`, don't set defaults. (Used for the
        second parse pass, so that options given after the mode word don't
        clobber the ones given before it.)
    """
    default = argparse.SUPPRESS if suppress_defaults else None
    false = argparse.SUPPRESS if suppress_defaults else False

    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__
    )
    parser.add_argument(
        "-V",
        dest="show_tmux_version",
        action="store_true",
        help="Print the version of the tmux interface that pymux emulates.",
    )
    parser.add_argument(
        "-S",
        "--socket",
        dest="socket",
        metavar="SOCKET",
        default=default,
        help="Unix socket path. (A number is accepted as well.)",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="filename",
        metavar="FILE",
        default=default,
        help="Path to configuration file. By default: '~/.pymux.conf'.",
    )
    parser.add_argument(
        "-d",
        "--detach-others",
        dest="detach_others",
        action="store_true",
        default=false,
        help="Detach all other clients, when attaching.",
    )
    parser.add_argument(
        "--log",
        dest="logfile",
        metavar="FILE",
        default=default,
        help="Logfile.",
    )
    parser.add_argument(
        "--truecolor",
        action="store_true",
        default=false,
        help="Render true color (24 bit) instead of 256 colors.",
    )
    parser.add_argument(
        "--ansicolor",
        action="store_true",
        default=false,
        help="Use only the 16 ANSI colors.",
    )


def _build_parser(with_positionals: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pymux",
        description="pymux: Pure Python terminal multiplexer.",
        epilog="Any other arguments are sent to the running server as a "
        "pymux command. Example: pymux split-window",
    )
    _add_options(parser, suppress_defaults=False)
    if with_positionals:
        # Collect the mode/command and all its arguments. Everything after
        # the first positional argument is taken verbatim.
        parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def _socket_from_env_warning() -> None:
    print("Please be careful nesting pymux sessions.")
    print("Unset PYMUX environment variable first.")


def run() -> None:
    a = _build_parser().parse_args()

    rest = a.args
    mode = None
    command = None

    if rest and rest[0] in MODES:
        # A mode word was given. The options after the mode word are parsed
        # as well. (argparse.REMAINDER above collected them verbatim.)
        mode = rest[0]
        mode_parser = argparse.ArgumentParser(
            prog="pymux", description="pymux: Pure Python terminal multiplexer."
        )
        _add_options(mode_parser, suppress_defaults=True)
        mode_args, extra = mode_parser.parse_known_args(rest[1:])
        for key, value in vars(mode_args).items():
            setattr(a, key, value)
        rest = extra

    if mode in MODES_WITH_A_FIRST_PANE:
        # An optional command can be given for the first pane.
        command = " ".join(shlex.quote(x) for x in rest) if rest else None
    elif mode is not None and rest:
        # A mode with extra arguments: e.g. `pymux list-sessions -F ...`.
        # The whole thing is sent to the server as one command.
        command = " ".join(shlex.quote(x) for x in (mode, *rest))
        mode = None
    elif rest:
        # Not a mode: all arguments form one pymux command.
        command = " ".join(shlex.quote(x) for x in rest)

    socket_name = a.socket or os.environ.get("PYMUX")
    socket_name_from_env = not a.socket and bool(os.environ.get("PYMUX"))
    filename = a.filename
    true_color = a.truecolor
    ansi_colors_only = a.ansicolor or bool(
        os.environ.get("PROMPT_TOOLKIT_ANSI_COLORS_ONLY", False)
    )

    # Parse pane_id from socket_name. It looks like "socket_name,pane_id".
    pane_id = None
    if socket_name and "," in socket_name:
        socket_name, pane_id = socket_name.rsplit(",", 1)

    # Color depth. Without a flag the client asks its terminal and
    # falls back through COLORTERM and TERM. (See `pymux.colors`.)
    if ansi_colors_only:
        color_depth = ColorDepth.DEPTH_4_BIT
    elif true_color:
        color_depth = ColorDepth.DEPTH_24_BIT
    else:
        color_depth = None

    # Expand socket name. (Make it possible to just accept numbers.)
    if socket_name and socket_name.isdigit():
        socket_name = "%s/pymux.sock.%s.%s" % (
            tempfile.gettempdir(),
            getpass.getuser(),
            socket_name,
        )

    # Configuration filename.
    default_config = os.path.abspath(os.path.expanduser("~/.pymux.conf"))
    if not filename and os.path.exists(default_config):
        filename = default_config

    if filename:
        filename = os.path.abspath(os.path.expanduser(filename))

    # Store the configuration file name. (The daemonized server, started by
    # `new-session`, needs it after the fork.)
    global _current_filename
    _current_filename = filename

    # Setup logging.
    if a.logfile:
        logging.basicConfig(filename=a.logfile, level=logging.DEBUG, force=True)

    if a.show_tmux_version:
        # Like `tmux -V`. Tools like libtmux parse this to know which tmux
        # features the command line supports. Pymux implements the command
        # line interface of tmux 3.4.
        print("tmux 3.4")
        sys.exit(0)

    if mode == "standalone":
        # When a command was given (e.g. 'pymux standalone htop'), run it in
        # the first pane.
        mux = Pymux(source_file=filename, startup_command=command)
        mux.run_standalone(
            color_depth=color_depth or ColorDepth.DEPTH_8_BIT
        )

    elif mode == "integrated":
        if socket_name_from_env:
            _socket_from_env_warning()
            sys.exit(1)

        # A server and one client in this process. The client reads a
        # queue that this server writes, so it reaches this server and
        # no other one.
        mux = Pymux(source_file=filename, startup_command=command)

        # Only when a socket was asked for. The user interface never
        # reads it; it is there so that `pymux -S <socket> <command>`
        # and libpymux reach this server.
        if socket_name:
            mux.listen_on_socket(socket_name)

        # No depth of its own. Like `attach`, this passes on what the
        # flags asked for, and `None` leaves the answer to the probe of
        # the terminal and to the environment.
        mux.run_integrated(
            color_depth=color_depth,
            detach_other_clients=a.detach_others,
        )

    elif mode in ("list-sessions", "ls"):
        if socket_name:
            # With an explicit socket, ask the server. (The exit code tells
            # whether there is a session. Like tmux.)
            sys.exit(_send_command(socket_name, "list-sessions", pane_id))

        clients = list(list_clients())
        for c in clients:
            print(c.socket_name)
        if not clients:
            # Like tmux, exit with a non-zero exit code when there is no
            # server running.
            sys.exit(1)

    elif mode == "start-server":
        if socket_name_from_env:
            _socket_from_env_warning()
            sys.exit(1)

        # Log to stdout, when no logfile was given.
        if not a.logfile:
            logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

        # Create 'Pymux'. (Do this after the logging setup, so that crashes
        # in Pymux() can be logged.)
        mux = Pymux(source_file=filename)

        # Run server.
        socket_name = mux.listen_on_socket(socket_name)
        try:
            mux.run_server()
        except KeyboardInterrupt:
            sys.exit(1)

    elif mode == "attach":
        if socket_name_from_env:
            _socket_from_env_warning()
            sys.exit(1)

        detach_other_clients = a.detach_others

        if socket_name:
            create_client(socket_name).attach(
                detach_other_clients=detach_other_clients, color_depth=color_depth
            )
        else:
            # Connect to the first server.
            for c in list_clients():
                c.attach(
                    detach_other_clients=detach_other_clients, color_depth=color_depth
                )
                break
            else:  # Nobreak.
                print("No pymux instance found.")
                sys.exit(1)

    elif command and socket_name:
        # Run command in the given session.
        sys.exit(_send_command(socket_name, command, pane_id))

    elif command:
        # A command was given, but no socket was given. Try to send it to the
        # first running server. (Like 'tmux split-window' without a target.)
        for c in list_clients():
            sys.exit(c.run_command(command, pane_id))
        else:
            print("No pymux instance found.")
            sys.exit(1)

    elif not socket_name:
        # Run client/server combination.
        mux = Pymux(source_file=filename)
        socket_name = mux.listen_on_socket(socket_name)
        pid = daemonize()

        if pid > 0:
            # Create window. It is important that this happens in the daemon,
            # because the parent of the process running inside should be this
            # daemon. (Otherwise the `waitpid` call won't work.)
            mux.run_server()
        else:
            create_client(socket_name).attach(color_depth=color_depth)

    else:
        if socket_name_from_env:
            _socket_from_env_warning()
            sys.exit(1)
        else:
            print("Invalid command.")
            sys.exit(1)


def _no_server_error(socket_name: str | None) -> None:
    "Print the 'no server running' error, like tmux does."
    sys.stderr.write(
        "no server running on %s\n"
        % (socket_name or os.path.join(tempfile.gettempdir(), "pymux.sock.*"),)
    )


def _send_command(socket_name: str, command: str, pane_id=None) -> int:
    """
    Send a command to the server, print the answer and return the exit code.

    Some commands are handled by the client itself:
    - `new-session`: starts a new server when there is none yet.
    - `kill-session` and `kill-server`: stop the server.
    """
    args = shlex.split(command)
    name = args[0] if args else ""

    if name == "new-session":
        return _new_session(socket_name, command, args, pane_id)
    elif name in ("kill-session", "kill-server"):
        return _kill(socket_name, command)
    else:
        try:
            client = create_client(socket_name)
        except OSError:
            _no_server_error(socket_name)
            return 1
        return client.run_command(command, pane_id)


def _flag_args(args: List[str], flags_with_value: Tuple[str, ...]) -> Tuple[
    Set[str], Dict[str, str], List[str]
]:
    """
    Parse a list of short flags, given either glued to their value
    (e.g. `-sname`) or as a separate argument (e.g. `-s name`).
    """
    flags: Set[str] = set()
    values: Dict[str, str] = {}
    positional: List[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and len(arg) >= 2 and not arg.startswith("--"):
            flag = arg[1]
            rest = arg[2:]
            if flag in flags_with_value:
                if rest:
                    values[flag] = rest
                    i += 1
                elif i + 1 < len(args):
                    values[flag] = args[i + 1]
                    i += 2
                else:
                    i += 1
            else:
                flags.add(flag)
                i += 1
        else:
            positional.append(arg)
            i += 1

    return flags, values, positional


def _wait_for_server(socket_name: str, timeout: float = 5.0) -> bool:
    "Wait until the server accepts connections on this socket."
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(socket_name):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(socket_name)
                    return True
                finally:
                    s.close()
            except OSError:
                pass
        time.sleep(0.05)
    return False


def _new_session(
    socket_name: str, command: str, args: List[str], pane_id=None
) -> int:
    """
    Handle `new-session`. Start a new server when there is no server yet.
    Otherwise, pass the command to the running server. (Which will report
    a duplicate session error, like tmux does.)
    """
    flags, values, positional = _flag_args(
        args[1:], flags_with_value=("s", "F", "c", "n", "x", "y", "e")
    )
    attach = "d" not in flags
    session_name = values.get("s")
    start_directory = values.get("c")
    window_name = values.get("n")
    format_str = values.get("F")
    print_info = "P" in flags
    window_command = " ".join(positional) or None

    startup_command = " ".join(x for x in (window_name, window_command) if x) or None

    # Is there a server running on this socket?
    server_running = _wait_for_server(socket_name, timeout=0.1)

    if server_running:
        # Pass to the server. (Pymux has one session per server. This will
        # give a duplicate session error.)
        try:
            client = create_client(socket_name)
        except OSError:
            _no_server_error(socket_name)
            return 1
        return client.run_command(command, pane_id)

    # Start a new daemonized server.
    if start_directory:
        try:
            os.chdir(os.path.abspath(os.path.expanduser(start_directory)))
        except OSError:
            pass

    mux = Pymux(
        source_file=filename_var(),
        startup_command=startup_command,
        session_name=session_name,
    )
    socket_name = mux.listen_on_socket(socket_name)

    pid = daemonize()
    if pid > 0:
        # This is the daemon. Run the server until all panes are gone.
        mux.run_server()
        return 0

    # Wait for the server to come up.
    if not _wait_for_server(socket_name):
        _no_server_error(socket_name)
        return 1

    if print_info:
        # Ask the server to format the session information.
        client = create_client(socket_name)
        query = "list-sessions -F %s" % shlex.quote(format_str or "#{session_id}")
        return client.run_command(query)

    if attach:
        client = create_client(socket_name)
        client.attach(color_depth=ColorDepth.DEPTH_8_BIT)
        return 0

    return 0


def _kill(socket_name: str, command: str) -> int:
    """
    Handle `kill-session` and `kill-server`.
    """
    if not _wait_for_server(socket_name, timeout=0.1):
        _no_server_error(socket_name)
        return 1

    try:
        client = create_client(socket_name)
    except OSError:
        _no_server_error(socket_name)
        return 1
    return client.run_command(command)


if __name__ == "__main__":
    run()
