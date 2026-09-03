"""
Tests for the environment that a pane runs in.

A pane is not the terminal that the client attached from. A program
that reads the name of the outer terminal from the environment picks a
protocol that the pane may not speak, so a pane does not inherit it.
"""
import pytest

from pymux.environment import (
    TERMINAL_IDENTITY_VARIABLES,
    scrub_terminal_identity,
)


@pytest.mark.parametrize(
    "name",
    [
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "KITTY_WINDOW_ID",
        "KONSOLE_VERSION",
        "ITERM_SESSION_ID",
        "WEZTERM_EXECUTABLE",
        "GHOSTTY_RESOURCES_DIR",
        "WT_SESSION",
        "VTE_VERSION",
        "ALACRITTY_WINDOW_ID",
    ],
)
def test_a_variable_that_names_the_terminal_goes(name):
    environment = {name: "something", "HOME": "/home/someone"}
    scrub_terminal_identity(environment)
    assert name not in environment
    assert environment["HOME"] == "/home/someone"


def test_the_rest_of_the_environment_stays():
    environment = {
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "SHELL": "/bin/sh",
        "KITTY_LISTEN_ON": "unix:/tmp/kitty",
        "KITTY_WINDOW_ID": "1",
    }
    scrub_terminal_identity(environment)
    assert environment == {
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "SHELL": "/bin/sh",
        # A remote control socket names a service, not the terminal
        # that the pane draws in. A program that uses one still
        # reaches the right window.
        "KITTY_LISTEN_ON": "unix:/tmp/kitty",
    }


def test_scrubbing_an_environment_without_them_changes_nothing():
    environment = {"TERM": "xterm-256color"}
    scrub_terminal_identity(environment)
    assert environment == {"TERM": "xterm-256color"}


def test_every_name_is_listed_once():
    assert len(set(TERMINAL_IDENTITY_VARIABLES)) == len(TERMINAL_IDENTITY_VARIABLES)


def test_the_names_are_upper_case():
    "An environment variable of a terminal is upper case, every time."
    for name in TERMINAL_IDENTITY_VARIABLES:
        assert name == name.upper()
