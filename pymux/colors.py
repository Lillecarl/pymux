"""
What the outer terminal of a client does with colour.

Two facts, both about one terminal and neither about the session, so
both are asked of that terminal at attach time and kept per client:
**how many colours it takes**, and **which two it draws with when
nothing says otherwise**.

## How many colours it takes

Terminals differ in how many colours they take, and a multiplexer that
guesses too high paints the wrong colours on a weak terminal. The
client therefore asks its terminal, and falls back through weaker
answers when it stays quiet:

1. The probe. The client sets a 24 bit colour and asks the terminal to
   report the graphic rendition back (`DECRQSS`). A terminal that keeps
   the three components answers with them; one that reduces the colour
   answers with the index it picked instead.
2. `COLORTERM`. The usual environment signal for 24 bit colour.
3. `TERM`. A name with "256color" in it takes 256 colours, a small set
   of old names takes 16, and a dumb terminal takes none.

A command line flag beats all of it: the user knows their terminal.

## Which two it draws with

`OSC 10` reports the default foreground and `OSC 11` the default
background, and a terminal answers both with an `XParseColor` spec.
pymux draws a status line and title bars over a background it did not
choose, so what that background is decides whether its own colours
land on it or fight it.

**Only a client can find this out**, and that is the whole shape of
Lillecarl/pymux#223: the theme is one attribute of the server today,
so two people on one session get the same colours whatever their
terminals are. This module answers the factual half -- what this
terminal is -- and nothing here decides what to do about it.
"""

import re
from typing import List

from prompt_toolkit.output import ColorDepth
from prompt_toolkit.utils import is_dumb_terminal
from pyte.colors import Color, DEFAULT_COLORS, PALETTE, parse_color
from pyte.osc import DYNAMIC_COLOR_CODES, QUERY, ColorBase, Osc
from pyte.sequences import osc

__all__ = [
    "COLOR_QUERIES",
    "TRUECOLOR_PROBE",
    "ColorDetection",
    "DefaultColors",
]

# The three components of the probe colour. They are small and
# distinct, so an approximation cannot report them by accident.
PROBE_RED, PROBE_GREEN, PROBE_BLUE = 1, 2, 3

# Set the probe colour, ask what the graphic rendition is now, and put
# the attributes back. The reply is a DCS string sequence.
TRUECOLOR_PROBE = "\x1b[38;2;%i;%i;%im\x1bP$qm\x1b\\\x1b[0m" % (
    PROBE_RED,
    PROBE_GREEN,
    PROBE_BLUE,
)

#: The names this module reads a dynamic colour reply for. The others
#: that `DYNAMIC_COLOR_CODES` numbers are the cursor and the selection,
#: which pymux does not draw with.
_WANTED_COLORS = ("foreground", "background")

#: The codes to ask for, in the order they go out.
_COLOR_CODES = tuple(
    code for code, name in DYNAMIC_COLOR_CODES.items() if name in _WANTED_COLORS
)

#: Ask the outer terminal which two colours it draws with when nothing
#: says otherwise, and which sixteen it paints the palette's first
#: entries with. Each reply comes back on the same code, with an
#: `XParseColor` spec in place of the question mark.
#:
#: The cube and the grey ramp beyond sixteen are convention: every
#: terminal that numbers 256 colours paints them the same way, so they
#: are not asked about. The sixteen ANSI colours are the ones a theme
#: decides, and they are what a pane has to know to answer its
#: programs with what the person sees. Lillecarl/pymux#283.
#:
#: A terminal that does not answer says nothing and holds nothing up:
#: the device attributes reply closes the detection, and these go out
#: before it.
COLOR_QUERIES = "".join(osc(code, QUERY) for code in _COLOR_CODES) + "".join(
    osc(Osc.PALETTE_COLOR, "%i;?" % index) for index in range(16)
)

# The reply of a DECRQSS request: "DCS <valid> $ r <answer> ST". The
# validity digit is not read: terminals disagree about which value
# means valid, and the answer itself says whether the colour survived.
_DECRQSS_REPLY_RE = re.compile(r"^\x1b[P\x90]\d*\$r(.*?)(?:\x1b\\|\x9c)$", re.DOTALL)

# A 24 bit colour in the answer. The components follow the "2" either
# after semicolons or after colons, with an empty colour space id in
# the colon form.
_TRUECOLOR_RE = re.compile(r"38[:;]2[:;]([\d:;]*)")

# Terminals that are known to take only the sixteen ANSI colours.
_ANSI_ONLY_TERMS = frozenset(
    [
        "ansi",
        "cygwin",
        "eterm-color",
        "linux",
        "vt100",
        "vt220",
        "vt320",
    ]
)


def reports_truecolor(reply: str) -> bool:
    """
    True when a DECRQSS reply gives the three components of the probe
    colour back. That means the terminal kept them.
    """
    match = _DECRQSS_REPLY_RE.match(reply)
    if match is None:
        return False

    found = _TRUECOLOR_RE.search(match.group(1))
    if found is None:
        return False

    numbers = [int(part) for part in re.split(r"[:;]", found.group(1)) if part != ""][
        :4
    ]
    probe = [PROBE_RED, PROBE_GREEN, PROBE_BLUE]

    # The colon form may carry a colour space id before the three
    # components, and the semicolon form may be followed by further
    # attributes. Both shapes put the components in the first four
    # numbers.
    return numbers[:3] == probe or numbers[1:4] == probe


class ColorDetection:
    """
    What colour depth the terminal of one client takes.

    :param forced: The depth that the user asked for on the command
        line. It wins over everything that the terminal says.
    """

    def __init__(self, forced: ColorDepth | None = None) -> None:
        self.forced = forced
        self.term = ""
        self.colorterm = ""

        #: True once the probe came back with its own colour.
        self.truecolor = False

    def handle_reply(self, data: str) -> None:
        "Read what a terminal reply says about the colours."
        if reports_truecolor(data):
            self.truecolor = True

    @property
    def depth(self) -> ColorDepth:
        "The depth to render with."
        if self.forced is not None:
            return self.forced
        if self.truecolor:
            return ColorDepth.DEPTH_24_BIT
        return depth_from_environment(self.term, self.colorterm)


class DefaultColors:
    """
    The two colours the terminal of one client draws with by itself.

    `None` for either one means the terminal did not say. Most do:
    `OSC 11` is answered by every terminal this collection is judged
    against. A terminal that stays quiet is not a fault and not a
    special case -- the caller has a theme, and a theme is what it
    falls back to.

    **This is a fact about a terminal, not a preference of a person.**
    Which theme somebody chose is the other half of
    Lillecarl/pymux#223, and nothing here has an opinion about it.
    """

    def __init__(self) -> None:
        #: What the terminal writes text in.
        self.foreground: Color | None = None

        #: What the terminal leaves behind text. This is the one pymux
        #: draws its own chrome over.
        self.background: Color | None = None

        #: The sixteen colours the terminal paints the palette's first
        #: entries with, learned from the same handshake. An entry that
        #: the terminal did not say stays `None`, and a pane then
        #: answers with the conventional one. The cube beyond sixteen
        #: is convention in every terminal, so it is not asked about.
        self.ansi: List[Color | None] = [None] * 16

    def __repr__(self) -> str:
        return "DefaultColors(foreground=%r, background=%r)" % (
            self.foreground,
            self.background,
        )

    def handle_osc_reply(self, code: str, payload: str) -> bool:
        """
        Read one OSC reply, and say whether it learned a colour.

        The caller has already split the code from the payload, because
        it routes other codes of its own. A reply this cannot read --
        another code, or a spec `XParseColor` does not name -- changes
        nothing and answers false.
        """
        if code == Osc.PALETTE_COLOR:
            return self._learn_ansi(payload.strip())

        name = DYNAMIC_COLOR_CODES.get(code)
        if name not in _WANTED_COLORS:
            return False

        color = parse_color(payload.strip())
        if color is None:
            return False

        setattr(self, name, color)
        return True

    def _learn_ansi(self, payload: str) -> bool:
        """
        Read one palette reply: "index ; spec".

        The ask named the first sixteen only, so an index beyond them
        is a reply nobody asked for and reads nothing. An index that
        did learn a colour answers true, because the panes should be
        told again.
        """
        index, _, spec = payload.partition(";")
        if not index.isdigit() or int(index) >= len(self.ansi):
            return False
        color = parse_color(spec.strip())
        if color is None:
            return False
        self.ansi[int(index)] = color
        return True

    def color_base(self) -> ColorBase:
        """
        What a pane starts with when this terminal is the one drawing
        it: the sixteen colours that were learned over the
        conventional cube, and the two defaults that were learned over
        the ones `pyte.colors` reports. An entry the terminal did not
        say leaves the conventional one standing.

        This is the answer a pane gives a program that asks, and it
        describes the theme that the person in front of this terminal
        is looking at. Lillecarl/pymux#283.
        """
        palette = [
            learned or PALETTE[index] for index, learned in enumerate(self.ansi)
        ] + list(PALETTE[16:])
        defaults = dict(DEFAULT_COLORS)
        if self.foreground is not None:
            defaults["foreground"] = self.foreground
        if self.background is not None:
            defaults["background"] = self.background
        return ColorBase(palette, defaults)


def depth_from_environment(term: str, colorterm: str) -> ColorDepth:
    """
    The colour depth that the environment of a client suggests, for a
    terminal that answered no probe.
    """
    colorterm = (colorterm or "").lower()
    if colorterm in ("truecolor", "24bit"):
        return ColorDepth.DEPTH_24_BIT

    term = (term or "").lower()
    if not term or is_dumb_terminal(term):
        return ColorDepth.DEPTH_1_BIT
    if "direct" in term:
        return ColorDepth.DEPTH_24_BIT
    if "256color" in term:
        return ColorDepth.DEPTH_8_BIT
    if term in _ANSI_ONLY_TERMS:
        return ColorDepth.DEPTH_4_BIT

    # Anything else is a terminal from this century: 256 colours are a
    # safe floor, and the probe would have said more.
    return ColorDepth.DEPTH_8_BIT
