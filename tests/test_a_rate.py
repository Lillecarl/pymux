"""
A rate reads the last few seconds of a count, in a unit that fits.

The rules this judges: the first read has nothing to compare with and
reads zero; later reads divide the bytes since a sample by the seconds
since it; a count that stopped reads zero once a window has passed;
and a rate prints in B/s, kB/s, MB/s or GB/s, with a decimal only
under ten. Lillecarl/pymux#678.
"""

from __future__ import annotations

import pytest

from pymux.rate import RATE_WINDOW, Rate, per_second


def test_the_first_read_is_zero():
    assert Rate().read(5000, 100.0) == 0.0


def test_a_rate_divides_the_bytes_since_a_sample_by_the_seconds():
    rate = Rate()
    rate.read(0, 100.0)
    assert rate.read(4000, 104.0) == 1000.0


def test_a_count_that_stopped_reads_zero_a_window_later():
    rate = Rate()
    for second in range(10):
        rate.read(second * 1000, 100.0 + second)
    stopped = 9000
    assert rate.read(stopped, 110.0) > 0
    assert rate.read(stopped, 110.0 + RATE_WINDOW + 1) == 0.0


def test_reads_many_times_a_second_keep_few_samples():
    rate = Rate()
    for tick in range(1000):
        rate.read(tick, 100.0 + tick / 100)
    assert len(rate._samples) <= RATE_WINDOW + 2


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
