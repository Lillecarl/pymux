"""
pymux's AI plugins: features that ask a model about what pymux shows.

Each feature is a subpackage, and each plugin is an entry point in the
`pymux.plugins` group. `observe` is what they share: a look at a pane.

- `claude_code_encourager` nudges a Claude Code agent that stopped when
  it could have gone on alone.
"""

__version__ = "0.1"
