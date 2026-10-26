"""
Which keys the status line and copy mode take.

tmux reads the editor of the person and switches both `status-keys` and
`mode-keys` to vi for a vi. pymux held them at emacs whatever the
person used, so the vi keys of copy mode reached nothing: `v`, `y` and
`$` do nothing in a read-only buffer with emacs keys, and a person
pressing them read that as copy mode being unable to copy.
Lillecarl/pymux#375.

Helix gives Helix keys on the line, and copy mode stays on emacs keys
until it has Helix keys of its own. Lillecarl/pymux#749.
"""

from __future__ import annotations

import pytest

from pymux.enums import EditingKeys
from pymux.main import Pymux
from pymux.utils import editor_keys

#: What the editor of a person says, and the keys it means. `nvim` is
#: the case that says why tmux tests for the substring and not for the
#: start of the name.
EDITORS = [
    ("vi", EditingKeys.VI),
    ("vim", EditingKeys.VI),
    ("nvim", EditingKeys.VI),
    ("/usr/bin/vim", EditingKeys.VI),
    ("/run/current-system/sw/bin/nvim", EditingKeys.VI),
    ("hx", EditingKeys.HELIX),
    ("helix", EditingKeys.HELIX),
    ("/run/current-system/sw/bin/hx", EditingKeys.HELIX),
    ("hx --vsplit", EditingKeys.HELIX),
    ("hxd", EditingKeys.EMACS),
    ("emacs", EditingKeys.EMACS),
    ("/usr/bin/emacs", EditingKeys.EMACS),
    ("nano", EditingKeys.EMACS),
    ("code --wait", EditingKeys.EMACS),
    ("", EditingKeys.EMACS),
]


@pytest.mark.parametrize(("editor", "keys"), EDITORS)
def test_the_editor_says_which_keys(editor, keys):
    assert editor_keys({"EDITOR": editor}) is keys


def test_nothing_in_the_environment_gives_emacs_keys():
    assert editor_keys({}) is EditingKeys.EMACS


def test_visual_answers_before_editor():
    "tmux asks for VISUAL first and takes it, empty or not."
    assert editor_keys({"VISUAL": "nano", "EDITOR": "vim"}) is EditingKeys.EMACS
    assert editor_keys({"VISUAL": "vim", "EDITOR": "nano"}) is EditingKeys.VI


def test_the_path_of_an_editor_does_not_decide():
    "Only the name does. A path that holds 'vi' is not an answer."
    assert editor_keys({"EDITOR": "/home/vim/bin/nano"}) is EditingKeys.EMACS


def test_helix_gives_helix_keys_on_the_line_and_in_copy_mode(monkeypatch):
    monkeypatch.setenv("EDITOR", "hx")
    monkeypatch.delenv("VISUAL", raising=False)
    pymux = Pymux()
    assert pymux.session_defaults.status_keys is EditingKeys.HELIX
    assert pymux.session_defaults.mode_keys is EditingKeys.HELIX
