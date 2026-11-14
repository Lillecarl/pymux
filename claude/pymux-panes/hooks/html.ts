/**
 * Reads `pymux capture-pane -H --compact`, which `pyte/pyte/compact_html.py`
 * writes: one `pre`, a row a line, `span` for colour, background and dim
 * (`opacity:.5`), `b`/`i`/`u`/`s` for renditions, `a href` for links. Reverse
 * video is already resolved into the colours. Only `&` and `<` are escaped.
 */
import type { Run, Screen } from '../types'

type Style = Omit<Run, 'text'>

const TOKEN = /<(\/?)([a-z]+)([^>]*)>|([^<]+)/g
const RENDITIONS: Record<string, keyof Style> = { b: 'bold', i: 'italic', u: 'underline', s: 'strikethrough' }

function unescape(text: string): string {
  return text.replace(/&lt;/g, '<').replace(/&amp;/g, '&')
}

function attribute(attrs: string, name: string): string | undefined {
  return new RegExp(`${name}="([^"]*)"`).exec(attrs)?.[1]
}

function declarations(attrs: string): Record<string, string> {
  const style: Record<string, string> = {}
  for (const declaration of (attribute(attrs, 'style') ?? '').split(';')) {
    const [property, value] = declaration.split(':')
    if (property && value) style[property.trim()] = value.trim()
  }
  return style
}

export function parseScreen(html: string): Screen {
  const screen: Screen = { rows: [[]] }
  const stack: Style[] = [{}]
  let inPre = false
  for (const [, closing, name, attrs = '', text] of html.matchAll(TOKEN)) {
    if (text !== undefined) {
      if (!inPre) continue
      text.split('\n').forEach((part, index) => {
        if (index > 0) screen.rows.push([])
        if (part) screen.rows.at(-1)?.push({ ...stack.at(-1), text: unescape(part) })
      })
      continue
    }
    if (name === 'pre') {
      inPre = !closing
      if (inPre) {
        const style = declarations(attrs)
        screen.color = style.color
        screen.background = style.background
      }
      continue
    }
    if (closing) {
      if (stack.length > 1) stack.pop()
      continue
    }
    const style: Style = { ...stack.at(-1) }
    const rendition = name === undefined ? undefined : RENDITIONS[name]
    if (rendition) {
      Object.assign(style, { [rendition]: true })
    } else if (name === 'span') {
      const declared = declarations(attrs)
      if (declared.color) style.color = declared.color
      if (declared.background) style.background = declared.background
      if (declared.opacity) style.dim = true
    } else if (name === 'a') {
      const href = attribute(attrs, 'href')
      if (href) style.href = unescape(href)
    }
    stack.push(style)
  }
  return screen
}

/** The rows worth drawing in `room` rows: trailing blank rows dropped, the bottom kept. */
export function visibleRows(screen: Screen, room: number): Run[][] {
  let end = screen.rows.length
  while (end > 0 && (screen.rows[end - 1] ?? []).every(run => !run.text.trim() && !run.background)) end--
  return screen.rows.slice(Math.max(0, end - room), end)
}
