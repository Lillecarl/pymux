"""
Every error the server logged while a measurement ran.

A measurement that logged one is red even when every number is in
budget. A redraw that raised in the background passed pymux-busy for
weeks (Lillecarl/pymux#580), and a stub with no `forward_osc` filled
pymux-leaks with tracebacks while it passed.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator


class LoggedErrors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))

    def complaint(self) -> str:
        "What to say about them, or nothing when there were none."
        if not self.records:
            return ""
        return "The server logged %d errors:\n%s" % (len(self.records), "\n".join(self.records[:5]))


@contextlib.contextmanager
def logged_errors() -> Iterator[LoggedErrors]:
    errors = LoggedErrors()
    logging.getLogger().addHandler(errors)
    try:
        yield errors
    finally:
        logging.getLogger().removeHandler(errors)
