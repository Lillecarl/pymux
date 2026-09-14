"""
Which keys the status line and copy mode take.

tmux reads the editor of the person and switches both `status-keys` and
`mode-keys` to vi for a vi. pymux held them at emacs whatever the
person used, so the vi keys of copy mode reached nothing: `v`, `y` and
`$` do nothing in a read-only buffer with emacs keys, and a person
pressing them read that as copy mode being unable to copy.
Lillecarl/pymux#375.
"""

import pytest

from pymux.utils import keys_are_vi

#: What the editor of a person says, and whether it means vi keys.
#: `nvim` is the case that says why tmux tests for the substring and
#: not for the start of the name.
EDITORS = [
    ("vi", True),
    ("vim", True),
    ("nvim", True),
    ("/usr/bin/vim", True),
    ("/run/current-system/sw/bin/nvim", True),
    ("emacs", False),
    ("/usr/bin/emacs", False),
    ("nano", False),
    ("code --wait", False),
    ("", False),
]


@pytest.mark.parametrize("editor, vi", EDITORS)
def test_the_editor_says_which_keys(editor, vi):
    assert keys_are_vi({"EDITOR": editor}) is vi


def test_nothing_in_the_environment_gives_emacs_keys():
    assert keys_are_vi({}) is False


def test_visual_answers_before_editor():
    "tmux asks for VISUAL first and takes it, empty or not."
    assert keys_are_vi({"VISUAL": "nano", "EDITOR": "vim"}) is False
    assert keys_are_vi({"VISUAL": "vim", "EDITOR": "nano"}) is True


def test_the_path_of_an_editor_does_not_decide():
    "Only the name does. A path that holds 'vi' is not an answer."
    assert keys_are_vi({"EDITOR": "/home/vim/bin/nano"}) is False
