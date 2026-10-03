import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.notifications import Urgency


def notify(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Record a notification for the hub to show.

    The title names it and the body says the rest, the way
    `notify-send` takes a summary and a body: the shim calls this,
    and so does a person testing the hub. The pane is the one the
    client being told looks at, or none when no client does -- a
    notification from a script has no pane, and the hub says so.
    """
    urgency = {"low": Urgency.LOW, "normal": Urgency.NORMAL, "critical": Urgency.CRITICAL}[
        args.urgency
    ]

    pane_id = None
    try:
        asking = pymux.the_client_to_tell()
        focused = pymux.focused_pane_of(asking) if asking is not None else None
        pane_id = focused.pane_id if focused is not None else None
    except Exception:
        pane_id = None

    pymux.notification_center.add(
        title=args.title, body=args.body, urgency=urgency, pane_id=pane_id
    )


def register(subparsers):
    parser = add_command(subparsers, notify)
    parser.add_argument("title", metavar="<title>", help="What the notification is.")
    parser.add_argument(
        "body", metavar="<body>", nargs="?", default="", help="What else it says."
    )
    parser.add_argument(
        "-u",
        dest="urgency",
        choices=("low", "normal", "critical"),
        default="normal",
        help="How insistently it asks.",
    )
