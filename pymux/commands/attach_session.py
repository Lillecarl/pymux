import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command, not_past_this_client
from pymux.commands.sessions import move_this_client


def attach_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Attach the calling client to a session of this server.

    A client is attached already -- the words arrive over the socket of
    a server it is on -- so this moves it, which is what tmux's
    attach-session does for a client that is inside a session. Without
    `-t` it goes to the session a person looked at last.

    `-d` takes the session from the other clients that hold it. `-x`
    does that and hangs up the process each of them was started by, so
    the terminal it was in closes rather than going back to a shell
    prompt. tmux spells both, and reads `-x` as `-d` with a harsher
    message. Lillecarl/pymux#347.

    `-r` makes this client one that only watches, and takes it out of
    the size of the plane at the same time: a person watching from a
    phone must not shrink the session for the people working in it.
    **It only sets.** Attaching again without `-r` leaves a read-only
    client read-only, which is what tmux does
    (`cmd-attach-session.c:118` sets both flags and clears neither).
    Lillecarl/pymux#467.

    **A client that only watches may not run `-d` or `-x`.** tmux
    lets it: the loop at `cmd-attach-session.c:127` has no read-only
    guard, so a watcher there takes the session from everybody and
    `-x` closes the terminal each of them was in.
    """
    not_past_this_client(pymux, args.d or args.x)

    client_state = move_this_client(
        pymux, args.target_session, detach_others=args.d, hang_up_others=args.x
    )

    if args.r:
        client_state.read_only = True
        client_state.ignore_size = True


def register(subparsers):
    parser = add_command(subparsers, attach_session, read_only=True)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to attach to.")
    parser.add_argument("-d", dest="d", action="store_true", help="Detach the other clients of that session.")
    parser.add_argument("-x", dest="x", action="store_true", help="Detach them, and hang up the process each one was started by.")
    parser.add_argument("-r", dest="r", action="store_true", help="Only watch: type nothing, and do not shrink the session.")
