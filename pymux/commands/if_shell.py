import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


import anyio

from pymux.commands import add_command, handle_command
from pymux.format import format_pymux_string


def if_shell(pymux: "Pymux", args: argparse.Namespace):
    """
    Run one command or the other, by what a shell answers.

    `if-shell 'test -f ~/.tmux.conf' "set -g status off" "set -g status on"`:
    the shell command runs, and when it exits zero the first command
    runs, else the second, when there is one. -F asks the question of
    a format instead of a shell: no program runs, and a non-empty
    answer is yes.

    **A question for a shell is a question this answers later.** The
    client that asked waits for it and the server does not, which is
    what `run-shell` beside this says. -F asks a format, which is
    answered here and now. Lillecarl/pymux#297, #311.
    """
    if args.F:
        return _then_run(pymux, args, bool(format_pymux_string(pymux, args.shell_command)))

    return _ask_the_shell(pymux, args)


async def _ask_the_shell(pymux: "Pymux", args: argparse.Namespace) -> None:
    # Captured and dropped. Only the status is read, and the output
    # would otherwise go to whatever the server's stdout is: /dev/null
    # under a daemon, and the person's own terminal in the integrated
    # and standalone routes, where it draws over the frame. tmux sends
    # it to /dev/null for the same reason. Lillecarl/pymux#312.
    done = await anyio.run_process(args.shell_command, check=False)

    answer = _then_run(pymux, args, done.returncode == 0)
    if answer is not None:
        await answer


def _then_run(pymux: "Pymux", args: argparse.Namespace, yes: bool):
    command = args.then_command if yes else args.else_command

    if command:
        # The answer of the command it chose is this command's answer:
        # `if-shell yes 'wait-for done'` waits, rather than leaving the
        # wait behind it.
        return handle_command(pymux, command)

    return None


def register(subparsers):
    parser = add_command(subparsers, if_shell)
    parser.add_argument("-F", dest="F", action="store_true", help="Ask a format, not a shell: no program runs, and a non-empty answer is yes.")
    parser.add_argument("-b", dest="b", action="store_true", help="Accepted for tmux and changes nothing: the server never waits for the shell, and the client that asked always does.")
    parser.add_argument("shell_command", metavar="<shell-command>", help="The question, through the shell, or the format with -F.")
    parser.add_argument("then_command", metavar="<then-command>", nargs="?")
    parser.add_argument("else_command", metavar="<else-command>", nargs="?")
