import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def pane_matches_session_name(pymux: "Pymux", target: str) -> bool:
    "Whether the given target matches the session. (For has-session.)"
    # No target asks whether the server has a session at all, which is
    # what `has-session` with no `-t` means in tmux. A server always has
    # one, so the answer is yes.
    if not target:
        return True

    # Accept an exact match ('=name' syntax as used by tmux/libtmux) or a
    # plain name. Only one session exists on a server.
    name = target[1:] if target.startswith("=") else target
    return name == pymux.session_name


def has_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Check whether the session exists.

    A session that is not there answers a non-zero exit code on the
    command line, which is what a script reads.
    """
    target = args.target_session or ""
    if not pane_matches_session_name(pymux, target):
        raise CommandException("can't find session: %s" % (target,))


def register(subparsers):
    parser = add_command(subparsers, has_session)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to look for.")
