"""
The recorded lists, read and written and judged.
"""

import pytest

from recorded import (
    how_to_record,
    lines,
    moved,
    read_counts,
    read_names,
    read_verdicts,
    write_list,
)


def test_comments_and_blank_lines_are_not_verdicts(tmp_path):
    "And a comment is a line that starts with a hash, not one that has one."
    path = tmp_path / "lists.txt"
    path.write_text(
        "# a comment\n"
        "\n"
        "the first screen 12\n"
        "the second screen #3 5\n"
        "  # an indented comment\n"
    )

    assert read_verdicts(path) == {
        "the first screen": "12",
        "the second screen #3": "5",
    }


def test_a_missing_file_is_no_list(tmp_path):
    "The first run of a check with no recording yet writes one, not fails."
    assert read_verdicts(tmp_path / "nothing.txt") == {}
    assert read_names(tmp_path / "nothing.txt") == set()


def test_a_line_that_names_nothing_is_a_fault(tmp_path):
    "A list whose line quietly named nothing would judge against nothing."
    path = tmp_path / "lists.txt"
    path.write_text("12\n")

    with pytest.raises(ValueError):
        read_verdicts(path)


def test_read_counts_counts(tmp_path):
    path = tmp_path / "budgets.txt"
    path.write_text("# why\nname of a shape 1204\nanother 3\n")

    assert read_counts(path) == {"name of a shape": 1204, "another": 3}


def test_read_names_names(tmp_path):
    path = tmp_path / "failures.txt"
    path.write_text("# known to fail\nDECAWL\nSOS\n")

    assert read_names(path) == {"DECAWL", "SOS"}


def test_lines_reads_back_what_write_list_wrote(tmp_path):
    path = tmp_path / "lists.txt"
    write_list(path, "# why\n\n", [("second", "2"), ("first", "1")])

    assert lines(path) == ["first 1", "second 2"]
    assert read_verdicts(path) == {"first": "1", "second": "2"}


def test_how_to_record_says_how(tmp_path):
    advice = how_to_record("pymux-frame-instructions", "frame-budgets.txt", "pymux/tests/frame-budgets.txt")

    assert advice == [
        "# This is what the run saw. To make it what the check expects:",
        "#     nix build --file . checks.pymux-frame-instructions.run",
        "#     cp result/frame-budgets.txt pymux/tests/frame-budgets.txt",
    ]


def test_moved_says_which_way():
    "Up is a count that climbed, and down is a budget nobody updated."

    assert moved(110, 100) == 10.0
    assert moved(90, 100) == -10.0
    assert moved(110, 0) == 11000.0
