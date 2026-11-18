"""
The Claude Code encourager: when a Claude Code agent in a pane says it
is done (OSC 7501 `state=done`), Claude Haiku looks at the pane and says
whether the agent stopped for a reason the person must settle, or could
have gone on alone. When it could, the encourager types the next step
into the pane, as the person would.

`encourager` holds the plugin, `verdict` what Haiku is asked and
answers, and `haiku` one pane's conversation with it.
"""

from __future__ import annotations

from .encourager import Encourager

__all__ = ["Encourager", "plugin"]

plugin = Encourager
