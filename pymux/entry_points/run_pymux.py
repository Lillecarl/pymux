#!/usr/bin/env python
"""
pymux: terminal multiplexer.

Usage:
    pymux [options] [integrated|start-server|attach|list-sessions|ls|find|diagnose|renice|web] [<command> ...]

Running pymux without arguments attaches to the newest server that is
running, and starts one (daemonized) to attach to when none is. With -S
it does the same for that socket. Any other command (e.g. ``split-window``) is sent to a
running server, like tmux does.

Modes:
    integrated     : Run a server and one client in this process. They talk
                     through queues, not through a socket, so the client
                     reaches the server this command started and nothing
                     else. With -S, the server also listens on that socket
                     for commands; a client cannot attach over it, because
                     this server draws on the terminal it runs in. One
                     process holds both halves, so Ctrl-Z suspends the
                     server too, and detaching ends the whole thing.
    start-server   : Run a server daemon that can be attached later on.
    attach         : Attach to a running session.
    list-sessions  : List all running sessions. ('ls' works as well.)

Options:
    -S SOCKET      : Unix socket path. A number is accepted as well; the
                     socket will be created in the temp directory. And
                     ssh://[user@]host[:port]/path names a socket on
                     another machine: the panes run there and are drawn
                     here.
    -f FILE        : Path to configuration file. Without it, the first of
                     '$XDG_CONFIG_HOME/pymux/pymux.conf' and
                     '~/.pymux.conf' that is there.
    -d             : Detach all other clients, when attaching.
    -x             : Detach them, and hang up the process each one was
                     started by, so the terminal it was in closes.
    -r             : Only watch, and change nothing.
    -n NAME        : What to call this terminal: 'desk', 'phone'. The
                     commands that take '-t <client>' take it, and it
                     lasts as long as this attachment.
    --port PORT    : `pymux web`: the port to serve on. 8080 by default.
    --bind ADDRESS : `pymux web`: what to listen on. 127.0.0.1 by
                     default, which is this machine only.
    --allow-input  : `pymux web`: let a viewer type into the pane. Off
                     by default.
    --log FILE     : Logfile.
    --log-level LEVEL : How much to log.
    --truecolor    : Render true color (24 bit) instead of 256 colors.
                      (Each client can set this separately.)
    --ansicolor    : Use only the 16 ANSI colors.
    --version      : Print version and exit.
    -V             : Print the version of the tmux interface that pymux
                     emulates.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import os
import shlex
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, assert_never

from pymux import log
from pymux.client import command_client, create_client, is_ssh_url, list_clients
from pymux.config import find_config
from pymux.entry_points.arguments import Mode, how_much_to_log, parse_arguments
from pymux.entry_points.completion import answer_completion
from pymux.options.scope import rerun_in_a_scope
from pymux.utils import daemonize

if TYPE_CHECKING:
    from pymux.client.terminal import TerminalClient
    from pymux.core.main import Pymux

__all__ = ["run"]


def _refuse_nesting(socket_name_from_env: bool) -> None:
    "Leave when the socket came from $PYMUX: this runs inside a pane."
    if socket_name_from_env:
        print("Please be careful nesting pymux sessions.")
        print("Unset PYMUX environment variable first.")
        sys.exit(1)


def _log_like_a_daemon(a: argparse.Namespace) -> None:
    """
    A server's log: the file `--logfile` names, or stdout. A daemon has
    no terminal to spoil, and `daemonize` sends its stdout to /dev/null;
    a person who wants to read it runs the server in the foreground.
    """
    wanted = how_much_to_log(a.log_level)
    if a.logfile:
        log.configure(a.logfile, wanted)
    else:
        logging.basicConfig(stream=sys.stdout, level=wanted)


def run() -> None:
    if os.environ.get("_ARGCOMPLETE"):
        answer_completion()

    a, mode, command = parse_arguments()

    socket_name = a.socket or os.environ.get("PYMUX")
    socket_name_from_env = not a.socket and bool(os.environ.get("PYMUX"))
    filename = a.filename
    true_color = a.truecolor
    ansi_colors_only = a.ansicolor or bool(os.environ.get("PROMPT_TOOLKIT_ANSI_COLORS_ONLY", False))

    # Parse pane_id from socket_name. It looks like "socket_name,pane_id".
    pane_id = None
    if socket_name and "," in socket_name:
        socket_name, pane_id = socket_name.rsplit(",", 1)

    # Color depth. Without a flag the client asks its terminal and
    # falls back through COLORTERM and TERM. (See `pymux.style.colors`.) It
    # is answered where a depth is used, so that a detached command
    # pays neither the question nor the import of the answer.
    # Lillecarl/pymux#392.

    # A machine is somewhere to attach to, never somewhere to listen.
    # `listen_on_socket` would try to bind a path called "ssh:" and
    # fail somewhere further in. Lillecarl/pymux#90.
    if is_ssh_url(socket_name) and mode in (Mode.INTEGRATED, Mode.START_SERVER):
        print("A server listens on this machine, so -S has to name a path here.")
        sys.exit(1)

    # Expand socket name. (Make it possible to just accept numbers.)
    if socket_name and socket_name.isdigit():
        socket_name = "%s/pymux.sock.%s.%s" % (
            tempfile.gettempdir(),
            getpass.getuser(),
            socket_name,
        )

    # Configuration filename. `-f` names one; without it, the first of
    # `config_paths` that is there.
    if not filename:
        filename = find_config()

    if filename:
        filename = os.path.abspath(os.path.expanduser(filename))

    attaching = Attaching(
        config_file=filename,
        chosen_name=a.client_name,
        read_only=a.read_only,
        hang_up_others=a.hang_up_others,
        detach_others=a.detach_others,
        ansi_colors_only=ansi_colors_only,
        true_color=true_color,
    )

    # Where the log goes. Never to the terminal: in `integrated` the
    # server shares one with the client that draws on it, and a logger
    # with no handler writes to `sys.stderr`, which is that terminal.
    # `pymux/log.py` says the rest.
    #
    # `start-server` sets this up for itself below, after it has forked.
    if mode not in (Mode.START_SERVER, Mode.RESUME_SERVER, Mode.CHECK_SNAPSHOT):
        log.configure(a.logfile, how_much_to_log(a.log_level))

    if a.show_tmux_version:
        # Like `tmux -V`. Tools like libtmux parse this to know which tmux
        # features the command line supports. Pymux implements the command
        # line interface of tmux 3.4.
        print("tmux 3.4")
        sys.exit(0)

    match mode:
        case Mode.INTEGRATED:
            _refuse_nesting(socket_name_from_env)

            # A server and one client in this process. The client reads a
            # queue that this server writes, so it reaches this server and
            # no other one.
            mux = _new_pymux(source_file=filename, startup_command=command)

            # Only when a socket was asked for. The user interface never
            # reads it; it is there so that `pymux -S <socket> <command>`
            # and libpymux reach this server. A client that asks to attach
            # over it is refused: this server draws on the terminal it runs
            # in. Lillecarl/pymux#159.
            if socket_name:
                mux.serving.listen_on_socket(socket_name)

            # No depth of its own. Like `attach`, this passes on what the
            # flags asked for, and `None` leaves the answer to the probe of
            # the terminal and to the environment.
            mux.serving.run_integrated(
                color_depth=_color_depth(ansi_colors_only, true_color),
                detach_other_clients=a.detach_others or a.hang_up_others,
                chosen_name=a.client_name,
                hang_up_others=a.hang_up_others,
                read_only=a.read_only,
            )

        case Mode.WEB:
            sys.exit(_web(socket_name, a))

        case Mode.SEND_FILE:
            # Written to this terminal, which in a pane is the pane: no
            # socket, so it works wherever iTerm2's protocol is read.
            from pymux.downloads import send_files

            sys.exit(send_files(a.send_paths, sys.stdout.buffer, sys.stderr))

        case Mode.IMGCAT:
            # Written to this terminal too, as the kitty graphics protocol.
            from pymux.imgcat import imgcat

            sys.exit(imgcat(a.imgcat_words, sys.stdin.buffer, sys.stdout.buffer, sys.stderr))

        case Mode.LIST_SESSIONS | Mode.LS:
            if socket_name:
                # With an explicit socket, ask the server. (The exit code tells
                # whether there is a session. Like tmux.)
                sys.exit(_send_command(socket_name, "list-sessions", pane_id, attaching))

            clients = list(list_clients())
            for c in clients:
                print(c.socket_name)
            if not clients:
                # Like tmux, exit with a non-zero exit code when there is no
                # server running.
                sys.exit(1)

        case Mode.FIND:
            # The socket a local `attach` without `-S` would take, newest
            # first. The client of `ssh://host` runs this over its exec
            # channel: the finding lives where the binding lives, so a
            # change in where servers bind cannot leave that client
            # looking in the old place. Lillecarl/pymux#90.
            from libpymux.sockets import servers_newest_first

            names = servers_newest_first()
            if names:
                print(names[0])
            else:
                sys.exit(1)

        case Mode.DIAGNOSE:
            # One report of what this machine sees, for a paste into a
            # support conversation. Read-only, and light: the imports of
            # the report are stdlib only. Lillecarl/pymux#391.
            from pymux.diagnose import as_json, diagnose, human

            report = diagnose(socket_name=socket_name, config_file=filename)
            print(as_json(report) if a.diagnose_json else human(report))

        case Mode.RENICE:
            # Every server of this user, or the one `-S` names. Not the
            # one of `$PYMUX`: in a pane, "every" still means every.
            # Lillecarl/pymux#673.
            from libpymux.sockets import servers_newest_first

            from pymux.niceness import DEFAULT_NICENESS, renice

            if a.socket and is_ssh_url(socket_name):
                print("pymux renice reaches the servers of this machine. Run it on the other one.")
                sys.exit(1)
            names = [socket_name] if a.socket and socket_name else servers_newest_first()
            niceness = DEFAULT_NICENESS if a.niceness is None else a.niceness
            sys.exit(renice(names, niceness))

        case Mode.START_SERVER:
            _refuse_nesting(socket_name_from_env)
            rerun_in_a_scope()

            _log_like_a_daemon(a)

            # Create 'Pymux'. (Do this after the logging setup, so that crashes
            # in Pymux() can be logged.)
            mux = _new_pymux(source_file=filename)

            # Run server.
            socket_name = mux.serving.listen_on_socket(socket_name)
            try:
                mux.serving.run_server()
            except KeyboardInterrupt:
                sys.exit(1)

        case Mode.CHECK_SNAPSHOT:
            from pymux.core import upgrade

            # A failure is a traceback on stderr and a status, which the
            # upgrading server logs and answers with. So is a warning.
            logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
            match a.upgrade_words:
                case [path]:
                    upgrade.check(path)
                case _:
                    sys.exit("usage: pymux %s PATH" % (Mode.CHECK_SNAPSHOT,))

        case Mode.RESUME_SERVER:
            from pymux.core import upgrade

            # Where the image before this one logged, which it passed
            # on. Its stdout is this process's, as `start-server` has it.
            _log_like_a_daemon(a)
            match a.upgrade_words:
                case [path]:
                    upgrade.resume(path)
                case _:
                    sys.exit("usage: pymux %s PATH" % (Mode.RESUME_SERVER,))

        case Mode.ATTACH:
            _refuse_nesting(socket_name_from_env)

            if socket_name:
                attaching.attach(create_client(socket_name))
            # Connect to the first server.
            for c in list_clients():
                attaching.attach(c)
            print("No pymux instance found.")
            sys.exit(1)

        case None:
            if command and socket_name:
                # Run command in the given session.
                sys.exit(_send_command(socket_name, command, pane_id, attaching))

            elif command:
                # No socket: the newest server that answers, like `tmux
                # split-window` without a target. With none, only
                # `new-session` has something to do -- it starts one.
                newest = None
                for answered in list_clients():
                    newest = answered.socket_name
                    answered.socket.close()
                    break
                if newest is None and shlex.split(command)[:1] != ["new-session"]:
                    print("No pymux instance found.")
                    sys.exit(1)
                sys.exit(_send_command(newest, command, pane_id, attaching))

            elif not socket_name:
                # Attach or create, as bare `tmux` does: the newest
                # server that answers, and a new one only when none
                # does. Two servers by accident is never what a person
                # who typed `pymux` twice meant. Lillecarl/pymux#417.
                for newest in list_clients():
                    attaching.attach(newest)
                _start_and_attach(None, filename, attaching)

            elif socket_name_from_env:
                _refuse_nesting(socket_name_from_env)

            elif is_ssh_url(socket_name):
                # Starting a server on the far machine when none is
                # there is Lillecarl/pymux#90's other half.
                attaching.attach(create_client(socket_name))

            else:
                # The server `-S` names, or a new one there.
                try:
                    named = create_client(socket_name)
                except OSError:
                    named = None
                if named is not None:
                    attaching.attach(named)
                _start_and_attach(socket_name, filename, attaching)

        case _:
            assert_never(mode)


def _start_and_attach(socket_name: str | None, filename: str | None, attaching: Attaching) -> NoReturn:
    "Start a server on `socket_name`, or on a new one with None, and attach to it."
    rerun_in_a_scope()
    mux = _new_pymux(source_file=filename)
    socket_name = mux.serving.listen_on_socket(socket_name)
    pid = daemonize()

    if pid > 0:
        # Create window. It is important that this happens in the daemon,
        # because the parent of the process running inside should be this
        # daemon. (Otherwise the `waitpid` call won't work.)
        mux.serving.run_server()
        sys.exit(0)
    attaching.attach(create_client(socket_name))


def _new_pymux(
    source_file: str | None,
    startup_command: str | None = None,
    session_name: str | None = None,
    size_with_no_client: tuple[int, int] | None = None,
) -> Pymux:
    """
    Build the thing that runs a server.

    `pymux.core.main` arrives here and not from the top of the module: a
    detached command never builds one, and what it imports instead is
    the whole cost this file was charged. Lillecarl/pymux#392.

    `size_with_no_client` is `(columns, rows)` or None. Two numbers and
    not a `Size`, because `Size` is the toolkit's and this module may not
    reach for the toolkit before it knows it is starting a server.
    """
    from prompt_toolkit.data_structures import Size

    from pymux.core.main import Pymux

    return Pymux(
        source_file=source_file,
        startup_command=startup_command,
        session_name=session_name,
        size_with_no_client=(
            None if size_with_no_client is None else Size(rows=size_with_no_client[1], columns=size_with_no_client[0])
        ),
    )


def _axis_of(values: dict[str, str], flag: str, standing: int) -> int:
    """
    One axis of `new-session -x -y`, on the route that starts a server.

    The command handler reads the same two flags for a server that is
    already running (`pymux/commands/new_session.py`). This route cannot
    use it: there is nothing to send the command to yet, and the size has
    to be known before the first pane starts.

    Raises `ValueError` with the message to print.
    """
    given = values.get(flag)
    if given is None:
        return standing
    if given == "-":
        import shutil

        # The terminal of whoever typed this, which is what tmux reads
        # "-" as. With no terminal `get_terminal_size` answers the
        # standing numbers, which is the answer tmux gives for no client.
        room = shutil.get_terminal_size(fallback=(80, 24))
        return room.columns if flag == "x" else room.lines
    try:
        wanted = int(given)
    except ValueError:
        raise ValueError("Expecting an integer: %s" % (given,))
    if wanted < 1:
        raise ValueError("A window is at least one cell.")
    return wanted


def _web(socket_name: str | None, a: argparse.Namespace) -> int:
    """
    `pymux web`: serve panes of a running server in a browser.

    **It runs beside the server and not inside it.** One stream per viewer
    over the socket the server already has, so the server process never
    imports a web library. That is what makes the web parts optional, and
    it is the same relay path a caller with a front end of its own uses.

    **Loopback and a token, both.** A browser sends no credentials on a
    websocket upgrade, so on loopback an `Origin` check is the only guard
    and a page may simply omit the header. So every upgrade carries the
    token printed here. Lillecarl/pymux#461.
    """
    import anyio

    from pymux.web.server import MISSING, a_token, serve, the_server

    try:
        server = anyio.run(the_server, socket_name)
        pane_ids = [pane.id for pane in anyio.run(server.panes)]
    except OSError as none:
        sys.stderr.write("pymux: %s\n" % (none,))
        return 1

    token = a_token()
    host = a.web_bind or "127.0.0.1"
    port = int(a.web_port or 8080)

    if host not in ("127.0.0.1", "::1", "localhost"):
        sys.stderr.write(
            "pymux: serving on %s, which is not only this machine. The token is "
            "the only thing between a stranger and this terminal, and it travels "
            "in plain text unless something else adds TLS.\n" % (host,)
        )

    # **Flushed, every line.** The token is the only way in and this
    # process then runs until it is stopped, so a buffered stdout holds the
    # one thing a person needs until the thing they need it for has ended.
    # Measured: redirected to a file, nothing appeared at all.
    def say(line: str) -> None:
        print(line, flush=True)

    def ready() -> None:
        """
        Said once the port is listening, and not before.

        **The URL prints after the bind.** Printed first, a bind that
        then failed -- something else on the port -- would leave a caller
        with an address and a token for a server that is not there, and
        the only sign an EADDRINUSE after them.
        """
        say("pymux web on http://%s:%d/?pane=<pane>&t=%s" % (host, port, token))
        say("  panes: %s" % (" ".join(pane_ids) or "none",))
        say("  the session, as a client sees it: http://%s:%d/?session&t=%s" % (host, port, token))
        if not a.web_allow_input:
            say("  showing only; --allow-input takes keys")

    try:
        anyio.run(
            serve,
            server.socket_path,
            host,
            port,
            token,
            bool(a.web_allow_input),
            ready,
        )
    except RuntimeError as missing:
        if str(missing) == MISSING:
            sys.stderr.write("pymux: %s\n" % (MISSING,))
            return 1
        raise
    except KeyboardInterrupt:
        pass
    return 0


def _color_depth(ansi_colors_only: bool, true_color: bool):
    """
    The depth the flags asked for, or `None`: the client then asks its
    terminal and falls back through COLORTERM and TERM
    (`pymux.style.colors`). The import rides along -- the answer is an enum
    of the toolkit, and a detached command draws nothing.
    """
    from prompt_toolkit.output import ColorDepth

    if ansi_colors_only:
        return ColorDepth.DEPTH_4_BIT
    if true_color:
        return ColorDepth.DEPTH_24_BIT
    return None


@dataclass(frozen=True)
class Attaching:
    """
    What a person said about the client they attach: every route that
    attaches one reads the same flags, `new-session` too.

    The colour depth is worked out at the attach and not here, because
    a detached command must not import the toolkit. Lillecarl/pymux#392.
    """

    config_file: str | None = None
    chosen_name: str | None = None
    read_only: bool = False
    hang_up_others: bool = False
    detach_others: bool = False
    ansi_colors_only: bool = False
    true_color: bool = False

    def attach(self, client: TerminalClient) -> NoReturn:
        """
        Attach `client` as asked, then end this process the way the
        attachment ended. The code it leaves with is the server's to name:
        a server that will not serve this client says so in an `exit`
        packet. Lillecarl/pymux#332.
        """
        client.config_file = self.config_file
        client.chosen_name = self.chosen_name
        client.hang_up_others = self.hang_up_others
        client.read_only = self.read_only
        # `-x` is `-d` with a harsher message, which is how tmux reads
        # it: `if (dflag || xflag)` in `cmd-attach-session.c:123`.
        # Lillecarl/pymux#347.
        client.attach(
            detach_other_clients=self.detach_others or self.hang_up_others,
            color_depth=_color_depth(self.ansi_colors_only, self.true_color),
        )
        _leave(client)


def _leave(client: TerminalClient) -> NoReturn:
    """
    End this process the way the attachment ended.

    A client that was told to hang up sends the signal here, after the
    terminal is back and before the process goes: the same order tmux
    holds (`client.c:410-416`), and for the same reason -- a signal
    sent while the alternate screen is up leaves the terminal as pymux
    had it. Lillecarl/pymux#347.
    """
    client.hang_up_the_parent()
    sys.exit(client.exit_code)


def _no_server_error(socket_name: str | None) -> None:
    "Print the 'no server running' error, like tmux does."
    sys.stderr.write(
        "no server running on %s\n" % (socket_name or os.path.join(tempfile.gettempdir(), "pymux.sock.*"),)
    )


def _send_command(socket_name: str | None, command: str, pane_id: str | None, attaching: Attaching) -> int:
    """
    Send a command to the server, print the answer and return the exit code.

    Some commands are handled by the client itself:
    - `new-session`: starts a new server when there is none yet, on a
      fresh socket when `socket_name` is None.
    - `kill-session` and `kill-server`: stop the server.
    """
    args = shlex.split(command)
    name = args[0] if args else ""

    if name == "new-session":
        return _new_session(socket_name, command, args, pane_id, attaching)
    if socket_name is None:
        _no_server_error(None)
        return 1
    if name in ("kill-session", "kill-server"):
        return _kill(socket_name, command)
    try:
        client = command_client(socket_name)
    except OSError:
        _no_server_error(socket_name)
        return 1
    return client.run_command(command, pane_id)


def _flag_args(
    args: list[str],
    flags_with_value: tuple[str, ...],
    flags_alone: tuple[str, ...],
) -> tuple[set[str], dict[str, str], list[str]]:
    """
    Parse a list of short flags, given either glued to their value
    (e.g. `-sname`) or as a separate argument (e.g. `-s name`).

    **A flag this does not know is an error**, and it raises
    `ValueError` naming the argument. Anything else was collected into
    a set nobody reads, and a `--long` one fell past the test
    altogether and became a word of the command the first pane runs.
    So `new-session --zzz-not-a-flag "sleep 60"` started a session and
    said nothing at all -- on the one route where nobody is watching
    stderr, the invocation that starts the server. The same line
    against a server that is already running reaches argparse in
    `call_command_handler` and is refused there.
    Lillecarl/pymux#458.

    **The two tables have to hold what the command's own parser holds**,
    because a person writes one line and the route it takes is not
    their choice: it depends on whether a server is up.

    Several flags in one argument are read the way getopt reads them,
    so `-dP` is both of them. Only the first was taken before, which is
    the same silent drop one character along.
    """
    flags: set[str] = set()
    values: dict[str, str] = {}
    positional: list[str] = []

    def unknown(arg: str) -> ValueError:
        return ValueError("unrecognized arguments: %s" % (arg,))

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--":
            # getopt's separator: the rest is the command, whatever it
            # starts with.
            positional.extend(args[i + 1 :])
            break

        if arg.startswith("--"):
            raise unknown(arg)

        if not (arg.startswith("-") and len(arg) >= 2):
            positional.append(arg)
            i += 1
            continue

        i += 1
        letters = arg[1:]
        while letters:
            flag, letters = letters[0], letters[1:]

            if flag in flags_alone:
                flags.add(flag)
                continue

            if flag not in flags_with_value:
                raise unknown("-" + flag)

            if letters:
                values[flag] = letters
            elif i < len(args):
                values[flag] = args[i]
                i += 1
            else:
                raise ValueError("argument -%s: expected one argument" % (flag,))
            break

    return flags, values, positional


def _wait_for_server(socket_name: str, timeout: float = 5.0) -> bool:
    """
    Wait until the server accepts connections on this socket.

    **A machine is not a path.** `ssh://host/path` names a socket on
    another machine, and no file of that name is here, so the test
    below would say no server forever: `pymux -S ssh://... kill-server`
    said "no server running" and stopped. There is nothing to wait for
    on this machine, and the client says soon enough whether the far
    side answers. Lillecarl/pymux#90.
    """
    if is_ssh_url(socket_name):
        return True

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
    socket_name: str | None, command: str, args: list[str], pane_id: str | None, attaching: Attaching
) -> int:
    """
    Handle `new-session`. Start a new server when there is no server yet.
    Otherwise, pass the command to the running server. (Which will report
    a duplicate session error, like tmux does.)
    """
    # **The same flags `new_session.register` declares**, because a
    # person writes one line and does not choose the route it takes:
    # whether this invocation starts the server or reaches one decides
    # which parser reads it, and the two have to answer alike.
    # Lillecarl/pymux#458.
    try:
        flags, values, positional = _flag_args(
            args[1:],
            flags_with_value=("s", "n", "c", "x", "y", "F", "J"),
            flags_alone=("d", "P"),
        )
    except ValueError as unreadable:
        sys.stderr.write("pymux: %s\n" % (unreadable,))
        return 1

    attach = "d" not in flags
    session_name = values.get("s")
    start_directory = values.get("c")
    window_name = values.get("n")
    format_str = values.get("F")
    print_info = "P" in flags
    window_command = " ".join(positional) or None

    startup_command = " ".join(x for x in (window_name, window_command) if x) or None

    # Read before a server is forked, so that a number nobody can read
    # leaves no server behind. The standing answer is tmux's, and
    # `pymux.core.session.DEFAULT_SIZE` is the same two numbers; it is not
    # imported here because that would import the server.
    # Lillecarl/pymux#459. Neither flag names nothing, and the clients'
    # `window-size` decides. Lillecarl/pymux#817.
    try:
        size_with_no_client = (
            None if "x" not in values and "y" not in values else (_axis_of(values, "x", 80), _axis_of(values, "y", 24))
        )
    except ValueError as unreadable:
        sys.stderr.write("pymux: %s\n" % (unreadable,))
        return 1

    # Is there a server running on this socket?
    server_running = socket_name is not None and _wait_for_server(socket_name, timeout=0.1)

    if server_running:
        # Pass to the server. (Pymux has one session per server. This will
        # give a duplicate session error.)
        try:
            client = command_client(socket_name)
        except OSError:
            _no_server_error(socket_name)
            return 1
        return client.run_command(command, pane_id)

    # Start a new daemonized server, in a scope of its own.
    rerun_in_a_scope()
    if start_directory:
        with contextlib.suppress(OSError):
            os.chdir(os.path.abspath(os.path.expanduser(start_directory)))

    mux = _new_pymux(
        source_file=attaching.config_file,
        startup_command=startup_command,
        session_name=session_name,
        size_with_no_client=size_with_no_client,
    )
    socket_name = mux.serving.listen_on_socket(socket_name)

    pid = daemonize()
    if pid > 0:
        # This is the daemon. Run the server until all panes are gone.
        mux.serving.run_server()
        return 0

    # Wait for the server to come up.
    if not _wait_for_server(socket_name):
        _no_server_error(socket_name)
        return 1

    if print_info:
        # Ask the server to format the session information.
        client = command_client(socket_name)
        query = "list-sessions -F %s" % shlex.quote(format_str or "#{session_id}")
        return client.run_command(query)

    if attach:
        attaching.attach(create_client(socket_name))

    return 0


def _kill(socket_name: str, command: str) -> int:
    """
    Handle `kill-session` and `kill-server`.
    """
    if not _wait_for_server(socket_name, timeout=0.1):
        _no_server_error(socket_name)
        return 1

    try:
        client = command_client(socket_name)
    except OSError:
        _no_server_error(socket_name)
        return 1
    return client.run_command(command)


if __name__ == "__main__":
    run()
