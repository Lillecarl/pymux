from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pymux.commands import CommandParser, add_command
from pymux.commands.common import the_pane
from pymux.link_rules import rules_for
from pymux.url_mode import UrlMode

if TYPE_CHECKING:
    from pymux.core.main import Pymux


def show_urls(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Label every link the pane shows; a label's letters open or copy it.

    Text the nearest `pymux.toml` up from the pane's directory makes a
    link of is labelled too: an issue number, say. `pymux/link_rules.py`
    says how. It works live and in copy mode. The last letter in upper case copies
    instead of opening, and Escape leaves. `pymux/url_mode.py` says how
    the labels go.
    """
    pane = the_pane(pymux, args.target_pane)
    found = rules_for(pane.current_path())
    mode = UrlMode.over(pane, found.rules)
    if not mode.targets:
        pymux.show_message("; ".join(found.errors) or "No links on this pane")
        return
    if found.errors:
        pymux.show_message("; ".join(found.errors))
    pymux.get_client_state().url_mode = mode
    pymux.invalidate()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_urls)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to label.")
