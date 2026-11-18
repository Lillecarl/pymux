from __future__ import annotations

import deferimport

# Before anything of pymux loads, so every import under it is lazy:
# completion spends 60% fewer instructions, a frame none more.
# `DEFERIMPORT=eager` turns it off. hypothesis and asyncssh register
# themselves on import. Lillecarl/pyterm#698. olefile is optional to
# Pillow, which loads a plugin in a `try` and drops it on ImportError:
# lazy, the missing module failed later, outside that `try`, and
# `Image.open` raised on a file that was no image. truststore's
# `__init__` imports `from ._api` and then runs `del _api`, a name only
# an eager import binds: lazy, every HTTPS client that httpx2 made in
# the server raised NameError (a pymux-ai plugin's).
deferimport.install("deny=hypothesis,asyncssh,olefile,truststore")
