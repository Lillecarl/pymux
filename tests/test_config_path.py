"""
Where pymux looks for its configuration file.

It read `~/.pymux.conf` and nothing else. A dotfile straight in the
home directory is what XDG exists to stop, and tmux, kitty, Ghostty and
Alacritty all read an XDG path now.

So `$XDG_CONFIG_HOME/pymux/pymux.conf` is read first, and
`~/.pymux.conf` second. The second one stays because it is the only
path pymux ever read: dropping it would break every configuration that
exists today.

`find_config` is separate from `run` for the reason `parse_arguments`
is. Everything after it starts a server, so this is the last place
where a mistake is still cheap.

Lillecarl/pymux#196.
"""

import pytest

from pymux.entry_points.run_pymux import config_paths, find_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    "A home directory of our own, with no configuration in it."
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def write(path, text="set-option base-index 1\n"):
    "One configuration file, and the directories above it."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ----------------------------------------------------------------------
# Which paths, and in which order.


def test_the_xdg_path_comes_first(home):
    assert config_paths() == [
        str(home / ".config" / "pymux" / "pymux.conf"),
        str(home / ".pymux.conf"),
    ]


def test_xdg_config_home_names_the_first_one(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))

    assert config_paths()[0] == str(tmp_path / "elsewhere" / "pymux" / "pymux.conf")


def test_a_relative_xdg_config_home_is_ignored(home, monkeypatch):
    "The specification says an implementation ignores one."
    monkeypatch.setenv("XDG_CONFIG_HOME", "config")

    assert config_paths()[0] == str(home / ".config" / "pymux" / "pymux.conf")


def test_an_empty_xdg_config_home_is_ignored(home, monkeypatch):
    "Which the specification says as well."
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    assert config_paths()[0] == str(home / ".config" / "pymux" / "pymux.conf")


# ----------------------------------------------------------------------
# Which one is chosen.


def test_no_configuration_at_all_is_no_file(home):
    assert find_config() is None


def test_the_dotfile_is_still_read(home):
    "Every configuration that exists today is this one."
    wanted = write(home / ".pymux.conf")

    assert find_config() == str(wanted)


def test_the_xdg_file_is_read(home):
    wanted = write(home / ".config" / "pymux" / "pymux.conf")

    assert find_config() == str(wanted)


def test_the_xdg_file_wins_over_the_dotfile(home):
    "Both are there, so the first of the two answers."
    wanted = write(home / ".config" / "pymux" / "pymux.conf")
    write(home / ".pymux.conf")

    assert find_config() == str(wanted)


def test_a_directory_with_the_name_is_not_a_configuration(home):
    """
    A directory called `pymux.conf` answers `os.path.exists` and then
    fails to open. Choosing it would read as a broken configuration
    rather than as no configuration, so the dotfile below it answers.
    """
    (home / ".config" / "pymux" / "pymux.conf").mkdir(parents=True)
    wanted = write(home / ".pymux.conf")

    assert find_config() == str(wanted)
