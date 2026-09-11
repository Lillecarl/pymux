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
from pymux.style_pygments import the_names  # noqa: E402
from photograph_the_chrome import CHROME, FIRST_KEY, PREFIX, keys, main  # noqa: E402

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_THEMES_OUT", "theme-pictures"))

#: Which fixtures and which terminals to run.
ONLY = os.environ.get("PYMUX_THEMES", "")
ONLY_TERMINALS = os.environ.get("PYMUX_THEMES_TERMINALS", "")

#: The program in the pane, beside this file.
DEMO = Path(__file__).parent / "demo_application.py"


def the_demo_keys():
    """
    Split the window in two, and run the demo in the pane that took
    the keyboard.

    The command is typed, so the pane shows a shell that received it
    and then the program that answered, which is what a pane looks
    like in use rather than at rest.
    """
    return keys(
        (FIRST_KEY, PREFIX),
        (0.4, b"%"),
        (0.8, ("python %s\n" % (shlex.quote(str(DEMO)),)).encode("ascii")),
    )


#: One fixture per theme, hand or pygments. The name is the one
#: `set-option theme` takes, after `theme-`.
FIXTURES = {}
for _name in sorted(THEMES):
    FIXTURES["theme-%s" % _name] = (
        CHROME + "set-option theme %s\n" % _name,
        the_demo_keys(),
    )
for _name in the_names():
    FIXTURES["theme-pygments-%s" % _name] = (
        CHROME + "set-option theme pygments:%s\n" % _name,
        the_demo_keys(),
    )

if __name__ == "__main__":
    raise SystemExit(
        main(
            fixtures=FIXTURES,
            only=ONLY,
            only_terminals=ONLY_TERMINALS,
            out=PICTURES,
        )
    )
