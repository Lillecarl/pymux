"""The recorded lists: one module for reading, writing and recording.

The pattern is the house style. A run leaves its list in `result/`,
the gate judges the next run against the recorded one, and a
difference in either direction fails: a number that climbed is the
fault the check is for, and one that fell is a budget nobody updated,
whose fix gets recorded.

One file, one shape: a name that may carry spaces, and a verdict that
is its last word, one pair a line. A line that starts with a hash is a
comment, and not a line with a hash anywhere in it -- a name can end
in one, because "#3" is how a walk numbers the third screen of an
item.

A list of names with no verdicts at all is the same idea minus the
last word: `read_names` reads it. `tests/instructions.py` in ptterm
holds its own copy of the counting helper on purpose, because the
repositories are separate and no test tree depends on the other's.
Everything this module serves is inside this one repository.
"""

from pathlib import Path


def lines(path):
    """
    The lines of a recorded list that are verdicts, and not comments.

    A missing file is no lines: the first run of a check that has no
    recording yet writes one instead of failing.
    """
    if not Path(path).is_file():
        return []
    found = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            found.append(line)
    return found


def read_verdicts(path):
    """
    The recorded verdict of each name, as {name: text}.

    The verdict is the line's last word, and the name is everything
    before it, spaces and all. A line with no name is a fault and not
    something to skip over: a list whose line quietly named nothing
    would judge a run against nothing.
    """
    verdicts = {}
    for line in lines(path):
        name, space, verdict = line.rpartition(" ")
        if not space or not name.strip():
            raise ValueError("a recorded list names nothing: %r" % line)
        verdicts[name.strip()] = verdict
    return verdicts


def read_counts(path):
    "The recorded count of each measurement, as {name: int}."
    return {name: int(verdict) for name, verdict in read_verdicts(path).items()}


def read_names(path):
    "A recorded list of names and nothing else, as a set."
    return set(lines(path))


def how_to_record(check, result, target):
    """
    The lines that say how a run's list becomes the check's.

    `check` is the derivation, `result` what it leaves in `result/`,
    and `target` the recorded list to replace with it.
    """
    return [
        "# This is what the run saw. To make it what the check expects:",
        "#     nix build --file . checks.%s.run" % check,
        "#     cp result/%s %s" % (result, target),
    ]


def write_list(path, header, rows):
    """
    A recorded list: the header, then a name and its verdict, sorted.

    `rows` are (name, verdict) pairs. A name may carry spaces, and the
    verdict is one word, which is what `read_verdicts` reads back.
    """
    text = header + "".join("%s %s\n" % (name, verdict) for name, verdict in sorted(rows))
    Path(path).write_text(text)


def moved(counted, budget):
    """
    How far a counted number is from its budget, in percent.

    The sign is the direction: up is a count that climbed, and down is
    a budget nobody updated.
    """
    return 100.0 * (counted - budget) / max(1, budget)
