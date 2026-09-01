#!/usr/bin/env python
"""
pymux: Pure Python terminal multiplexer.

Usage:
    pymux [options] [standalone|start-server|attach|list-sessions] [<command> ...]

Running pymux without arguments starts a server (daemonized) and attaches
a client to it. Any other command (e.g. ``split-window``) is sent to a
running server, like tmux does.

Modes:
    standalone     : Run as a standalone process. (for debugging, detaching
                     is not possible.)
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
import sys
import tempfile

from prompt_toolkit.output import ColorDepth
from pymux import __version__
from pymux.client import create_client, list_clients
from pymux.main import Pymux
from pymux.utils import daemonize

__all__ = ["run"]

MODES = ("standalone", "start-server", "attach", "list-sessions", "ls")


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

    if mode == "standalone":
        # An optional command can be given for the first pane.
        command = " ".join(rest) if rest else None
    elif rest:
        # Not a mode: all arguments form one pymux command.
        command = " ".join(rest)
        mode = None

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

    # Color depth.
    if ansi_colors_only:
        color_depth = ColorDepth.DEPTH_4_BIT
    elif true_color:
        color_depth = ColorDepth.DEPTH_24_BIT
    else:
        color_depth = ColorDepth.DEPTH_8_BIT

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

    # Setup logging.
    if a.logfile:
        logging.basicConfig(filename=a.logfile, level=logging.DEBUG, force=True)

    if mode == "standalone":
        # When a command was given (e.g. 'pymux standalone htop'), run it in
        # the first pane.
        mux = Pymux(source_file=filename, startup_command=command)
        mux.run_standalone(color_depth=color_depth)

    elif mode in ("list-sessions", "ls"):
        for c in list_clients():
            print(c.socket_name)

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
        create_client(socket_name).run_command(command, pane_id)

    elif command:
        # A command was given, but no socket was given. Try to send it to the
        # first running server. (Like 'tmux split-window' without a target.)
        for c in list_clients():
            c.run_command(command, pane_id)
            break
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


if __name__ == "__main__":
    run()
