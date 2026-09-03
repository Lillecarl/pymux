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
