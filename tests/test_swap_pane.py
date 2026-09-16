"""
The direction of swap-pane, pinned as a recorded tmux difference.

tmux's `swap-pane -U` swaps with the pane above; pymux's swaps with
the pane below. The difference is deliberate and lives in the ledger
(`tests/reference/tmux_compat/divergences.toml`), with the tmux 3.7c
probe beside it. The name ends in `_product_divergence` so the ledger
and the tests can find each other. Lillecarl/pymux#400.
"""

import pytest

from session import create_session


@pytest.fixture
async def pymux():
    async with create_session() as (pymux, _state):
        yield pymux


def run(pymux, command):
    "Run one command and give back what it said about it."
    from pymux.commands import handle_command

    before = len(pymux.message_log)
    handle_command(pymux, command)
    return list(pymux.message_log)[before:]


async def test_swap_pane_swaps_the_other_way_from_tmux_product_divergence(pymux):
    """
    -U takes the pane below and -D the one above, where tmux's -U is
    up and -D down. The pair is one divergence, recorded under one
    entry, with the tmux 3.7c probe beside it. Lillecarl/pymux#400.
    """
    run(pymux, "split-window -v")
    run(pymux, "split-window -v")
    window = pymux.arrangement.get_active_window()
    before = list(window.panes)

    run(pymux, "select-pane -t .1")
    run(pymux, "swap-pane -U")

    after = list(window.panes)
    # The pane below (the one after in index order) came up, ours went
    # down, and the selection rode with our pane. tmux's -U would have
    # sent us up. The pane above is untouched either way.
    assert after[1] == before[2]
    assert after[2] == before[1]
    assert after[0] == before[0]
    assert window.active_pane == before[1]

    run(pymux, "swap-pane -D")

    after = list(window.panes)
    # -D is the mirror: our pane back up, the pane below back down.
    # tmux's -D would have sent us down again.
    assert after[1] == before[1]
    assert after[2] == before[2]
    assert window.active_pane == before[1]
