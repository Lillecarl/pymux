import { describe, expect, test } from 'claude-code/testing'

import { parseScreen, visibleRows } from './html'

// A Claude Code pane, as pymux 0.15 captured it.
const CAPTURE =
  '<pre style="color:#cdd6f4;background:#181825">\n\n<span style="color:#ffffff">●</span> Yes, <b>this</b> works &amp; &lt;so>\n</pre>\n'

describe('parseScreen', () => {
  test('keeps one row a line, with the screen colours from the pre', () => {
    const screen = parseScreen(CAPTURE)
    expect(screen.rows.length).toBe(4)
    expect(screen.color).toBe('#cdd6f4')
    expect(screen.background).toBe('#181825')
  })

  test('reads colours, renditions and escapes into runs', () => {
    const row = parseScreen(CAPTURE).rows[2]
    expect(row).toEqual([
      { color: '#ffffff', text: '●' },
      { text: ' Yes, ' },
      { bold: true, text: 'this' },
      { text: ' works & <so>' },
    ])
  })

  test('nests a rendition inside a span, and reads dim, background and links', () => {
    const screen = parseScreen(
      '<pre><span style="color:#ff0000;background:#000000;opacity:.5"><u>a</u>b</span><a href="https://x.dev/?a=1&amp;b=2">l</a></pre>',
    )
    expect(screen.rows[0]).toEqual([
      { color: '#ff0000', background: '#000000', dim: true, underline: true, text: 'a' },
      { color: '#ff0000', background: '#000000', dim: true, text: 'b' },
      { href: 'https://x.dev/?a=1&b=2', text: 'l' },
    ])
  })
})

describe('visibleRows', () => {
  test('drops trailing blank rows and keeps the bottom that fits', () => {
    const screen = parseScreen('<pre>one\ntwo\nthree\n\n   \n</pre>')
    expect(visibleRows(screen, 2).map(row => row.map(run => run.text).join(''))).toEqual(['two', 'three'])
  })

  test('keeps a blank row that paints a background', () => {
    const screen = parseScreen('<pre>one\n<span style="background:#ff0000">   </span>\n</pre>')
    expect(visibleRows(screen, 5).length).toBe(2)
  })
})
