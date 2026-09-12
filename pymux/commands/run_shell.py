import argparse
import shlex
import subprocess
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import get_app, set_app

from pymux.commands import add_command
from pymux.commands.common import show_listing


def run_shell(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Run a command through the shell, and show what it says.

    tmux shows the output in a view a key dismisses; the listing here
    is the same view, drawn when the command is done. A command that
    came over the command line is waited for, the way tmux's CLI
    waits, and its output goes back on the channel the client reads.
    Lillecarl/pymux#297.
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

    def job():
        done = subprocess.run(
            shell_command, shell=True, capture_output=True, text=True
        )
        out = done.stdout + done.stderr

        # A thread must not touch the interface: hand the answer to
        # the loop the server runs on, under the app of the client
        # that asked, which the callback does not arrive knowing.
        pymux.loop.call_soon_threadsafe(show, out)

    if pymux.command_output is not None:
        # The command line waits for the answer, and it runs on the
        # loop already.
        done = subprocess.run(
            shell_command, shell=True, capture_output=True, text=True
        )
        show(done.stdout + done.stderr)
    else:
        threading.Thread(target=job, daemon=True).start()


def register(subparsers):
    parser = add_command(subparsers, run_shell)
    parser.add_argument("-b", dest="b", action="store_true", help="Accepted for tmux and changes nothing: the server never waits on the command.")
    parser.add_argument("shell_command", nargs=argparse.REMAINDER, metavar="<shell-command>", help="The command to run, through the shell.")
