from __future__ import annotations

import deferimport

# Before anything of pymux loads, so every import under it is lazy:
# completion spends 60% fewer instructions, a frame none more.
# `DEFERIMPORT=eager` turns it off. hypothesis and asyncssh register
# themselves on import. Lillecarl/pymux#696.
deferimport.install("deny=hypothesis,asyncssh")
