"""
A meter reads the last few seconds of a count, in a unit that fits.

The rules this judges: the first sample has nothing to compare with and
reads zero; later samples divide the count since an earlier one by the
seconds since it; a count that stopped reads zero once a window has
passed; and a rate prints in B/s, kB/s, MB/s or GB/s, with a decimal
only under ten. Lillecarl/pyterm#680, Lillecarl/pyterm#996. A frame
rate prints in fps. Lillecarl/pyterm#598.
"""

from __future__ import annotations

import pytest

from pymux.connection.rate import frames_per_second, per_second
from pymux.core.meters import RATE_WINDOW, Meter


def counted(*totals: int):
    "A meter whose count reads `totals` in turn, one a sample."
    left = list(totals)
    return Meter(lambda: left.pop(0))


def test_the_first_sample_is_zero():
    meter = counted(5000)
    meter.sample(100.0)
    assert meter.rate == 0.0


def test_a_rate_divides_the_count_since_a_sample_by_the_seconds():
    meter = counted(0, 1000, 2000)
    for second in range(3):
        meter.sample(100.0 + second)
    assert meter.rate == 1000.0


def test_a_count_that_stopped_reads_zero_a_window_later():
    seconds = 10 + int(RATE_WINDOW) + 1
    meter = counted(*[min(second, 9) * 1000 for second in range(seconds)])
    for second in range(10):
        meter.sample(100.0 + second)
    assert meter.rate > 0
    for second in range(10, seconds):
        meter.sample(100.0 + second)
    assert meter.rate == 0.0


def test_samples_older_than_the_window_go():
    meter = counted(*range(100))
    for second in range(100):
        meter.sample(100.0 + second)
    assert len(meter._samples) <= RATE_WINDOW + 2


@pytest.mark.parametrize(
    ("rate", "said"),
    [
        (0, "0 B/s"),
        (135.4, "135 B/s"),
        (999.6, "1.0 kB/s"),
        (1430, "1.4 kB/s"),
        (13_200, "13 kB/s"),
        (2_100_000, "2.1 MB/s"),
        (7_000_000_000_000, "7000 GB/s"),
    ],
)
def test_a_rate_picks_its_unit(rate, said):
    assert per_second(rate) == said


@pytest.mark.parametrize(("rate", "said"), [(0, "0.0 fps"), (0.25, "0.2 fps"), (9.96, "10 fps"), (60.2, "60 fps")])
def test_a_frame_rate_has_a_decimal_under_ten(rate, said):
    assert frames_per_second(rate) == said
