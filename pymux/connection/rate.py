"""
How a rate reads to a person. `pymux.core.meters` measures it.
Lillecarl/pyterm#680, Lillecarl/pyterm#996.
"""

from __future__ import annotations

__all__ = ["frames_per_second", "per_second"]

#: The units a rate picks from, each a thousand times the one before.
UNITS = ("B/s", "kB/s", "MB/s", "GB/s")


def frames_per_second(rate: float) -> str:
    "A frame rate as a person reads it: `0.3 fps`, `12 fps`."
    if rate < 9.95:
        return "%.1f fps" % (rate,)
    return "%d fps" % (round(rate),)


def per_second(rate: float) -> str:
    "A rate as a person reads it: `135 B/s`, `1.4 kB/s`, `13 MB/s`."
    step = 0
    # 999.5 and not 1000: it rounds to "1000 B/s" otherwise.
    while rate >= 999.5 and step < len(UNITS) - 1:
        rate /= 1000
        step += 1
    if rate < 9.95 and step:
        return "%.1f %s" % (rate, UNITS[step])
    return "%d %s" % (round(rate), UNITS[step])
