import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


import anyio

from prompt_toolkit.application.current import get_app, set_app

from pymux.commands import add_command
from pymux.commands.common import show_listing


def run_shell(pymux: "Pymux", args: argparse.Namespace):
    """
    Run a command through the shell, and show what it says.

    tmux shows the output in a view a key dismisses; the listing here
    is the same view, drawn when the command is done. A command that
    came over the command line is waited for, the way tmux's CLI
    waits, and its output goes back on the channel the client reads.
    Lillecarl/pymux#297.

    **The server does not wait, and the client does.** This is a
    handler that answers later: the shell runs in a task, so every
    pane keeps reading its pty and every other client keeps getting
    frames while it does. `pymux run-shell 'sleep 30'` used to stop
    all of that for thirty seconds. Lillecarl/pymux#311.
    """
    # The command is the rest of the line, whatever words it holds:
    # they go back together, with the quoting the shell read.
    shell_command = shlex.join(args.shell_command)

    app = get_app()

    def show(out):
        try:
            with set_app(app):
                show_listing(pymux, "run-shell", out.rstrip("\n"))
        except ValueError:
            pass  # The client that asked is gone.

    async def job() -> None:
        try:
            done = await anyio.run_process(shell_command, check=False)
            out = _text(done.stdout) + _text(done.stderr)
        except OSError as e:
            # A shell that is not there raises OSError. Say so where
            # the output would have gone: a person who saw an empty
            # listing and no reason for it learned nothing.
            # Lillecarl/pymux#311.
            out = "run-shell: %s: %s\n" % (shell_command, e)

        show(out)

    if args.b:
        # tmux's `-b`: the caller is not waiting, so the answer cannot
        # go back on its channel. It goes to the view of the client
        # that asked, which is what `command_output` of `None` means.
        async def background() -> None:
            pymux.command_output = None
            pymux.command_error = None
            await job()

        pymux.spawn_command(background())
        return None

    return job()


def _text(data) -> str:
    return data.decode("utf-8", "replace") if data else ""


def register(subparsers):
    parser = add_command(subparsers, run_shell)
    parser.add_argument("-b", dest="b", action="store_true", help="Run in the background: the command that asked does not wait, and the output goes to the view of the client that asked.")
    parser.add_argument("shell_command", nargs=argparse.REMAINDER, metavar="<shell-command>", help="The command to run, through the shell.")
