import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.common import add_format_arguments, chosen_format, show_listing
from pymux.format import format_pymux_string

#: What a line says when nobody asked for a format. tmux's own shape,
#: with the machine in place of the tty: a pymux client can be on
#: another machine, and a tty path of a machine you are not on names
#: nothing. `LIST_CLIENTS_TEMPLATE` in tmux's `cmd-list-clients.c`.
DEFAULT_FORMAT = (
    "#{client_hostname}: #{session_name} "
    "[#{client_width}x#{client_height} #{client_termname}]"
)


def list_clients(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    List the clients of this server, one to a line.

    With `-t`, only the clients of that session. With `-F` each line is
    that format, and with `-J` it is that template. Lillecarl/pymux#330.
    """
    session = None
    if args.target_session:
        session = pymux.get_session(args.target_session)
        if session is None:
            raise CommandException("can't find session: %s" % (args.target_session,))

    chosen = chosen_format(args, DEFAULT_FORMAT)

    lines = []
    for client in pymux.clients:
        if session is not None and client.session is not session:
            continue

        window = client.session.arrangement.get_active_window()
        lines.append(
            format_pymux_string(
                pymux,
                chosen.string,
                window=window,
                pane=window.active_pane if window is not None else None,
                session=client.session,
                client=client,
                language=chosen.language,
            )
        )

    if chosen.asked:
        # One line to a client, the way tmux answers, so that a reader
        # that enumerates them splits on newlines.
        for line in lines:
            pymux.print_command_line(line)
    else:
        show_listing(pymux, "list-clients", "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, list_clients, aliases=("lsc",))
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="Only the clients of this session.")
    add_format_arguments(parser, "Print this format for each client.")
