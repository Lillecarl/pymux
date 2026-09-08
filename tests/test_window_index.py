"""
The first window is number one.

tmux starts at zero, but a keyboard starts at one. `set-option
base-index 0` brings the tmux default back.
"""

from pymux.arrangement import Arrangement, Pane


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def _arrangement(windows=1, base_index=None):
    arrangement = Arrangement()
    if base_index is not None:
        arrangement.base_index = base_index
    for _ in range(windows):
        arrangement.create_window(Pane(terminal=_Fake()), set_active=False)
    return arrangement


def test_the_first_window_is_one():
    arrangement = _arrangement()
    assert [w.index for w in arrangement.windows] == [1]


def test_the_windows_after_it_count_up():
    arrangement = _arrangement(windows=3)
    assert [w.index for w in arrangement.windows] == [1, 2, 3]


def test_the_option_brings_the_tmux_default_back():
    arrangement = _arrangement(windows=3, base_index=0)
    assert [w.index for w in arrangement.windows] == [0, 1, 2]


# ----------------------------------------------------------------------
# Putting a window at an index. Lillecarl/pymux#191.


def _indexes(arrangement):
    return [w.index for w in arrangement.windows]


def _open(arrangement, index=None):
    arrangement.create_window(Pane(terminal=_Fake()), set_active=False, index=index)


def test_a_window_goes_where_it_is_asked_to():
    arrangement = _arrangement(windows=3)

    _open(arrangement, index=9)

    assert _indexes(arrangement) == [1, 2, 3, 9]


def test_an_index_that_is_taken_moves_the_ones_in_the_way_up():
    arrangement = _arrangement(windows=3)

    _open(arrangement, index=2)

    assert _indexes(arrangement) == [1, 2, 3, 4]


def test_only_the_run_that_is_in_the_way_moves():
    """
    A gap stops the walk, so a window a person put out of the way
    stays where they put it.
    """
    arrangement = _arrangement(windows=3)
    _open(arrangement, index=7)
    assert _indexes(arrangement) == [1, 2, 3, 7]

    _open(arrangement, index=2)

    assert _indexes(arrangement) == [1, 2, 3, 4, 7]


def test_a_window_at_the_end_moves_nothing():
    arrangement = _arrangement(windows=3)

    _open(arrangement, index=4)

    assert _indexes(arrangement) == [1, 2, 3, 4]


def test_no_index_still_takes_the_lowest_free_one():
    "Which is what a session being restored asks for."
    arrangement = _arrangement(windows=3)
    arrangement.windows[1].index = 8

    _open(arrangement)

    assert sorted(_indexes(arrangement)) == [1, 2, 3, 8]


def test_the_windows_stay_in_order_of_index():
    arrangement = _arrangement(windows=3)

    _open(arrangement, index=1)

    assert _indexes(arrangement) == sorted(_indexes(arrangement))
    assert _indexes(arrangement) == [1, 2, 3, 4]
