"""
A server's window management, in an sqlite file a new build can load.

A hot upgrade writes this, and the new build reads it back
(Lillecarl/pymux#399). `sql` reads the same tables under `live`,
built from the objects as a question starts (`pymux/query.py`), so
they are shaped for a person or an agent to query: one row per
session, window, split and pane, and a column named after the
`#{format}` variable where one exists. `pymux/options/tables.py` holds them.

**It runs synchronously, on purpose.** The pause of an upgrade is the
event loop standing still. A writer that awaited anything would let a
pane read between two tables, and the file would describe two moments.

**Screen state belongs to the transition.** `pane_programs`,
`screen_rows` and `screen_appearances` exist only in this file,
written while handing over and read once by the new build, and so does
`client_modes`. `sql` sees the window-management tables and not these.

What each class writes is declared in `WRITES`. `tests/test_snapshot.py`
holds it against every `Keep.SAVED` in the classes' `KEEP`, so a saved
field that no table holds fails a test instead of vanishing in an
upgrade.
"""

from __future__ import annotations
