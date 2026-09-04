"""
The entry of terminfo that a pane is told to use.

Naming an entry that is not installed is worse than naming xterm, so
nothing is claimed until the compiled entry is found.
"""
import os

import pytest

from pymux.terminfo import (
    DATABASE_VARIABLE,
    ENTRY_NAME,
    FALLBACK_NAME,
    add_to_environment,
    database,
    terminal_name,
)


@pytest.fixture
def compiled(tmp_path, monkeypatch):
    "A database that holds the entry, the way `tic` writes one."
    directory = tmp_path / ENTRY_NAME[0]
    directory.mkdir()
    (directory / ENTRY_NAME).write_bytes(b"not really an entry")
    monkeypatch.setenv(DATABASE_VARIABLE, str(tmp_path))
    return tmp_path


def test_no_database_names_no_entry(monkeypatch):
    monkeypatch.delenv(DATABASE_VARIABLE, raising=False)
    assert database() is None
    assert terminal_name() == FALLBACK_NAME


def test_a_database_that_is_not_there(monkeypatch, tmp_path):
    monkeypatch.setenv(DATABASE_VARIABLE, str(tmp_path / "nowhere"))
    assert database() is None
    assert terminal_name() == FALLBACK_NAME


def test_a_database_that_holds_no_entry(monkeypatch, tmp_path):
    "A directory is not enough: the entry itself has to be in it."
    monkeypatch.setenv(DATABASE_VARIABLE, str(tmp_path))
    assert database() is None
    assert terminal_name() == FALLBACK_NAME


def test_the_entry_is_found(compiled):
    assert database() == str(compiled)
    assert terminal_name() == ENTRY_NAME


def test_the_entry_under_a_hashed_directory(monkeypatch, tmp_path):
    "ncurses stores it under the hexadecimal of the first letter too."
    directory = tmp_path / ("%02x" % ord(ENTRY_NAME[0]))
    directory.mkdir()
    (directory / ENTRY_NAME).write_bytes(b"not really an entry")
    monkeypatch.setenv(DATABASE_VARIABLE, str(tmp_path))
    assert database() == str(tmp_path)


def test_a_pane_searches_our_database_first(compiled):
    environment = {}
    add_to_environment(environment)
    assert environment["TERMINFO_DIRS"] == "%s:" % compiled


def test_the_database_that_was_there_stays(compiled):
    "An empty entry in the list means the place the system keeps."
    environment = {"TERMINFO_DIRS": "/somewhere/else"}
    add_to_environment(environment)
    assert environment["TERMINFO_DIRS"] == "%s:/somewhere/else" % compiled


def test_nothing_is_added_without_an_entry(monkeypatch):
    monkeypatch.delenv(DATABASE_VARIABLE, raising=False)
    environment = {}
    add_to_environment(environment)
    assert environment == {}
