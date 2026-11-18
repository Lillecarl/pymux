# pymux-ai

pymux plugins that ask a model about what a pane shows. A plain pymux
has none of them. Build pymux with them:

```nix
pymux.withPlugins (ps: [ ps.pymux-ai ])
```

Each feature is a subpackage and a plugin of its own, in the
`pymux.plugins` entry-point group.

## The Claude Code encourager

`claude_code_encourager` watches each pane where Claude Code runs. When
the agent reports `done` (OSC 7501) and stays done for a few seconds,
the plugin shows Claude Haiku three things: the pane's text, a picture
of the pane, and the jobs still running in the agent's directory.
Haiku answers `stop` or `continue`. With `continue`, the plugin types
Haiku's next step into the pane, after `[encourager] `, as the person
would.

- Each pane has a conversation of its own with Haiku, so Haiku sees the
  agent's earlier stops and its own answers.
- The instructions never change, so they are the cached prefix of every
  request.
- At half of Haiku's context window, Haiku sums the conversation up,
  and the conversation starts again from the summary.
- After five nudges in a row the plugin leaves the pane alone, until
  Haiku answers `stop` or an hour passes.

The key is `ANTHROPIC_API_KEY` in the pymux server's environment.
Pictures go to `$XDG_STATE_HOME/pymux/encourager/attachments`.
