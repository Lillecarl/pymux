"""
A pane, streamed to something that is not a terminal.

**`protocol` is pure and takes no dependency.** It turns a screen into
the frames of Lillecarl/pymux#461 and back, and it is the whole of the
design; a transport carries those frames and decides nothing. That is
why the streaming command on pymux's own socket and the websocket server
beside it are two thin things over one protocol rather than two
protocols.

`server` is the websocket half, and it is the only part that needs
anything installed: `pymux[web]`. A caller with a front end of its own
does not want it -- it relays frames over the socket it already holds --
so nothing here is imported until something asks for it.

from __future__ import annotations
"""
