"""
A picture of every theme, with the demo application in the pane.

`photograph_the_chrome.py` photographs the chrome of the hand themes.
This walks every theme `set-option theme` takes - the two hand ones,
and every style pygments offers, catppuccin's four flavours among
them - and runs `demo_application.py` in a split pane under each, so
the whole gallery lands in `$out` and a person reads them side by
side. Lillecarl/pymux#194, Lillecarl/pymux#195.

    nix build --file . checks.pymux-theme-pictures.run

`PYMUX_THEMES` narrows the run to the fixtures whose name holds that
text, and `PYMUX_THEMES_TERMINALS` to the terminals whose name does.
Six terminals run: the three dark ones, and the same three on a light
background, because a theme that read on the black it was written on
may be unreadable on white.

Nothing here is judged. The pictures are for a person, the same way
the ones of chrome are.
"""

import os
import shlex
import sys
from pathlib import Path

# `tests/`, for the harness beside this file, and the directory above
# it, for `pymux` itself. Running a script puts the script's own
# directory on the path and not the one it was started from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymux.style import THEMES  # noqa: E402
from pymux.style_pygments import names  # noqa: E402
from photograph_the_chrome import demo_keys, main  # noqa: E402
from take_a_picture import LIGHT_TERMINALS, TERMINALS  # noqa: E402

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_THEMES_OUT", "theme-pictures"))

#: Which fixtures and which terminals to run.
ONLY = os.environ.get("PYMUX_THEMES", "")
ONLY_TERMINALS = os.environ.get("PYMUX_THEMES_TERMINALS", "")

#: One fixture per theme, hand or pygments. The name is the one
#: `set-option theme` takes, after `theme-`.
FIXTURES = {}
for _name in sorted(THEMES):
    FIXTURES["theme-%s" % _name] = (
        CHROME + "set-option theme %s\n" % _name,
        demo_keys(),
    )
for _name in names():
    FIXTURES["theme-pygments-%s" % _name] = (
        CHROME + "set-option theme pygments:%s\n" % _name,
        demo_keys(),
    )

#: A few of the base16 collection, by the same name. The whole
#: collection is 337 schemes; these four are the ones the gallery
#: reads, until the gallery itself is built per scheme and per
#: terminal as its own derivations. Lillecarl/pymux#282,
#: Lillecarl/pymux#284.
for _name in (
    "catppuccin-mocha",
    "gruvbox-material-dark-medium",
    "solarized-light",
    "default-dark",
):
    FIXTURES["theme-base16-%s" % _name] = (
        CHROME + "set-option theme base16:%s\n" % _name,
        demo_keys(),
    )

#: The palette half of the same story: with the screen painted, the
#: pane answers a program's queries with the scheme's sixteen, and
#: the demo's swatches are the scheme's. The one above is the
#: off half, where the pane follows the terminal. Lillecarl/pymux#283.
FIXTURES["theme-base16-painted-mocha"] = (
    CHROME
    + "set-option theme base16:catppuccin-mocha\n"
    + "set-option paint-screen on\n",
    demo_keys(),
)

#: The takeover's own picture. `paint-screen` draws the scheme's
#: background behind every cell the program left at a default one, so
#: the terminal's own colours are never seen: mocha fills the light
#: three as it fills the dark one, where without the option a light
#: terminal keeps its white. The theme's own picture in the loop above
#: is the off half of the pair, same theme, same keys.
#: Lillecarl/pymux#273.
FIXTURES["painted-screen"] = (
    CHROME
    + "set-option theme pygments:catppuccin-mocha\n"
    + "set-option paint-screen on\n",
    demo_keys(),
)

if __name__ == "__main__":
    raise SystemExit(
        main(
            fixtures=FIXTURES,
            only=ONLY,
            only_terminals=ONLY_TERMINALS,
            out=PICTURES,
            # The dark three and the light three. A theme that reads on
            # the black it was written on may be unreadable on white,
            # and the light schemes of pygments want the other end.
            terminals=TERMINALS + LIGHT_TERMINALS,
        )
    )
