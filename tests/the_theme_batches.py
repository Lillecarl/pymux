"""
The batches the theme gallery builds in, as JSON.

The gallery is a derivation per terminal and per batch of themes: nix
schedules the derivations, a rerun rebuilds only the combos that
failed, and a timeout kills one combo instead of the tail of the
whole gallery. Lillecarl/pymux#284. This prints the (terminal, batch
of themes) pairs the gallery builds -- one seat boot per batch, which
is what the cost of a combo is -- and the check reads it back with
`fromJSON`.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from photograph_the_themes import FIXTURES  # noqa: E402
from take_a_picture import LIGHT_TERMINALS, TERMINALS  # noqa: E402

BATCH = int(os.environ.get("PYMUX_THEME_BATCH", "12"))

names = sorted(FIXTURES)
batches = [names[start : start + BATCH] for start in range(0, len(names), BATCH)]
matrix = [
    {"terminal": terminal.name, "batch": number, "themes": themes}
    for terminal in TERMINALS + LIGHT_TERMINALS
    for number, themes in enumerate(batches)
]

with open(os.path.join(os.environ["PYMUX_MATRIX_OUT"], "matrix.json"), "w") as out:
    json.dump(matrix, out, indent=1)
