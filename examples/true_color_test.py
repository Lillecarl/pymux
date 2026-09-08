#!/usr/bin/env python
"""
Run this script inside 'pymux' in order to discover whether or not in supports
true 24bit color. It should display a rectangle with both red and green values
changing between 0 and 80.
"""

from __future__ import unicode_literals, print_function
from pyte import escape
from pyte.sequences import csi

i = 0
for r in range(0, 80):
    for g in range(0, 80):
        b = 1
        print("\x1b[0;48;2;%s;%s;%sm " % (r, g, b), end="")
        if i == 1000:
            break

    print(csi(escape.SGR, 0) + "   \n", end="")
print(csi(escape.SGR, 0) + "\r\n")
