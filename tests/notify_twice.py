"""
Two desktop notifications, a moment apart, then quiet: the program of
a window nobody looks at, for the picture of the notification boxes.
Lillecarl/pymux#826.
"""

from __future__ import annotations

import sys
import time

for title, body in (
    ("Build finished", "All 412 tests passed in 38 seconds."),
    ("Agent waits", "Claude asks whether to push main to origin, and waits for an answer."),
):
    time.sleep(0.5)
    sys.stdout.write("\x1b]777;notify;%s;%s\x07" % (title, body))
    sys.stdout.flush()
time.sleep(600)
