"""
The ledger of intentional tmux differences, and its own rule.

`tests/reference/tmux_compat/divergences.toml` records every
deliberate difference from tmux: what tmux does, what pymux does, the
test that pins it and the probe that proved it. The ledger's rule --
`unlisted_divergences_are_bugs` -- needs bookkeeping to hold: every
entry names a test that exists, and every `_product_divergence` test
is in the ledger. Lillecarl/pymux#385.
"""

import re
import tomllib
from pathlib import Path

TESTS = Path(__file__).parent
LEDGER = TESTS / "reference" / "tmux_compat" / "divergences.toml"


def _entries():
    with LEDGER.open("rb") as f:
        return tomllib.load(f)["divergence"]


def test_every_entry_names_a_test_that_exists():
    for entry in _entries():
        needle = "def %s(" % (entry["test"],)
        found = any(needle in p.read_text() for p in TESTS.glob("test_*.py"))
        assert found, "%s names no test" % (entry["name"],)


def test_every_product_divergence_test_is_in_the_ledger():
    listed = {entry["test"] for entry in _entries()}
    for p in TESTS.glob("test_*.py"):
        for name in re.findall(r"def (test_\w*_product_divergence)\(", p.read_text()):
            assert name in listed, "%s pins a divergence with no entry" % (name,)
