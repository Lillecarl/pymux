/** One stretch of a row that draws one way. Absent fields draw in the screen's own way. */
export type Run = {
  text: string
  color?: string
  background?: string
  bold?: true
  italic?: true
  underline?: true
  strikethrough?: true
  dim?: true
  href?: string
}

/** A pane's screen, a row a line, and the colours its `pre` names. */
export type Screen = { rows: Run[][]; color?: string; background?: string }

/** What a mirror shows: the pane's last screen, or why it stopped. */
export type Frame = { screen?: Screen; revision?: string; error?: string }

declare module 'claude-code' {
  interface PluginState {
    'pymux-panes': {
      /** By pymux pane id (`%5`). */
      frames: Shaped<Record<string, Frame>>
    }
  }
}
