"""
What a pane tells one program about its terminal that is not true, for
a program that decides by a terminal's name and not by what the
terminal says it can do.

A pane names itself `pymux(<version>)` when asked (XTVERSION). A program
that does not know that name falls back to whatever it does for an
unknown terminal, which can be the worse of two paths pymux supports.
Teaching every such program about pymux does not scale, so a rule here
names the program in the pane's foreground, the outer terminal, and the
name the pane answers with instead.

Every rule is a hack. It says why it exists, with the evidence, so it
can go when the program learns better.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pymux import __version__

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pymux.main import ClientState

__all__ = ["LIES", "PYMUX_VERSION", "Lie", "outer_term", "version"]

#: What a pane answers XTVERSION with when no rule matches.
PYMUX_VERSION = "pymux(%s)" % __version__


@dataclass(frozen=True)
class Lie:
    "Answer XTVERSION with `answer` to `program`, when the outer terminal's TERM holds `outer`."

    program: str
    outer: str
    answer: str
    why: str


LIES = (
    Lie(
        program="yazi",
        outer="kitty",
        answer="kitty(pymux %s)" % __version__,
        why=(
            "yazi picks its image driver by brand (yazi-adapter/src/drivers/drivers.rs). "
            "kitty's brand gets Kgp, Unicode placeholders; an unknown brand that answers "
            "both kitty graphics and sixel gets Sixel first, so the same yazi drew "
            "differently in kitty and in pymux in kitty. checks.pymux-scenarios, "
            "yazi-images. Lillecarl/pymux#593."
        ),
    ),
)


def version(program: str | None, outer_term: str | None) -> str:
    "What XTVERSION answers to `program`, in an outer terminal whose TERM is `outer_term`."
    # A process names itself by its argv[0], which may be a whole path.
    program = os.path.basename(program or "")
    asked = [lie for lie in LIES if lie.program == program]
    for lie in asked:
        if outer_term and lie.outer in outer_term:
            logger.info("Told %s the terminal is %r, in %r.", program, lie.answer, outer_term)
            return lie.answer
    if asked:
        logger.info("%s asked in %r, and no rule lies to it there.", program, outer_term)
    return PYMUX_VERSION


def outer_term(client: ClientState | None) -> str:
    """
    The TERM of the terminal `client` sits at. A client that came over a
    socket reported it; an integrated server is its own client, so its
    own environment says it.
    """
    term = client.connection.colors.term if client is not None else ""
    return term or os.environ.get("TERM", "")
