from __future__ import annotations

import deferimport

# Before anything of pymux loads, so every import under it is lazy:
# completion spends 60% fewer instructions, a frame none more.
# `DEFERIMPORT=eager` turns it off. hypothesis and asyncssh register
# themselves on import. Lillecarl/pymux#696. olefile is optional to
# Pillow, which loads a plugin in a `try` and drops it on ImportError:
# lazy, the missing module failed later, outside that `try`, and
# `Image.open` raised on a file that was no image.
deferimport.install("deny=hypothesis,asyncssh,olefile")
