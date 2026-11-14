import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Frame, Run } from '../types'
import { parseScreen, visibleRows } from './html'

// A Claude pane's id: `pymux-` and the pymux pane's number (`%5` is `pymux-5`).
const PREFIX = 'pymux-'
// A wait that answers this often lets a loop see that its pane was closed.
const WAIT_SECONDS = 5
// The least time between two captures: a pane streaming a build changes far
// more often than anyone reads, and one capture costs about 120 ms.
const FRAME_MS = 150
const PANE_ID = /^%\d+$/

const frames = atom({ plugin: 'pymux-panes', key: 'frames' } as const, {}, { shape: 'frames-1' })

// The panes a loop mirrors. A loop ends when its pane leaves this set.
const watched = new Set<string>()

type Ran = { code: number | null; stdout: string; stderr: string }

async function pymux($: EngineInterface, args: string[]): Promise<Ran> {
  const child = $.process.spawn({ argv: ['pymux', ...args] })
  let stdout = ''
  let stderr = ''
  for await (const { stream, text } of child) {
    if (stream === 'stdout') stdout += text
    else stderr += text
  }
  const { code } = await child.result

  return { code, stdout, stderr }
}

function failed(ran: Ran, what: string): string {
  return `${what} failed (exit ${ran.code}): ${ran.stderr.trim() || ran.stdout.trim()}`
}

async function setFrame($: EngineInterface, pane: string, frame: Frame): Promise<void> {
  await update($, frames, map => ({ ...map, [pane]: frame }))
}

/**
 * Mirrors one pane until it leaves `watched`: wait for a revision other than
 * the last one drawn, capture, draw. `--since 0` answers at once, so the first
 * frame does not wait for the pane to change.
 */
async function mirror($: EngineInterface, pane: string): Promise<void> {
  let since = '0'
  try {
    while (watched.has(pane)) {
      const waited = await pymux($, ['wait-pane-change', '-t', pane, '--since', since, '--timeout', String(WAIT_SECONDS)])
      if (waited.code !== 0) {
        await setFrame($, pane, { error: failed(waited, 'pymux wait-pane-change') })
        return
      }
      const revision = waited.stdout.trim()
      if (revision === since) continue
      since = revision
      const captured = await pymux($, ['capture-pane', '-H', '--compact', '-p', '-t', pane])
      if (captured.code !== 0) {
        await setFrame($, pane, { error: failed(captured, 'pymux capture-pane') })
        return
      }
      await setFrame($, pane, { screen: parseScreen(captured.stdout), revision })
      await $.clock.sleep(FRAME_MS)
    }
  } catch (error) {
    await setFrame($, pane, { error: `pymux: ${String(error)}` })
  } finally {
    watched.delete(pane)
  }
}

async function open($: EngineInterface, pane: string): Promise<boolean> {
  if (!watched.has(pane)) {
    watched.add(pane)
    void mirror($, pane)
  }
  const opened = await $.ui.open({ id: PREFIX + pane.slice(1), title: `pymux ${pane}` })

  return opened.isPlaced
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: 'pymux',
      description: 'Mirror a pymux pane live in a pane here; with no pane, list the panes',
      argumentHint: '%pane',
    })

    return started
  })

  on('command.run', { command: 'pymux' }, async ($, e) => {
    const pane = e.args.trim()
    if (!pane) {
      const listed = await pymux($, [
        'list-panes',
        '-a',
        '-F',
        '#{pane_id}  #{session_name}:#{window_index}.#{pane_index}  #{window_name}  #{pane_width}x#{pane_height}  #{pane_title}',
      ])
      if (listed.code !== 0) return { text: failed(listed, 'pymux list-panes') }

      return { text: `Run /pymux %N to mirror one:\n${listed.stdout.trimEnd()}` }
    }
    if (!PANE_ID.test(pane)) return { text: `pymux: "${pane}" is no pane id; a pane id reads %5` }
    const isPlaced = await open($, pane)

    return { text: isPlaced ? `Mirroring pymux pane ${pane}.` : `pymux pane ${pane} waits for room.` }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id.startsWith(PREFIX)) {
      const pane = `%${e.id.slice(PREFIX.length)}`
      watched.delete(pane)
      await update($, frames, map => {
        const { [pane]: _, ...rest } = map

        return rest
      })
    }

    return next(e)
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (!e.requestId.startsWith(PREFIX)) return next(e)
    const pane = `%${e.requestId.slice(PREFIX.length)}`
    const { Box, Text } = $.ui.resolve(e)
    const frame = (await read($, frames))[pane]
    if (frame?.error) return <Text color="error">{frame.error}</Text>
    if (!frame?.screen) return <Text dimColor>Waiting for pymux pane {pane}…</Text>
    const room = Math.max(1, e.props.scroll?.bodyRows ?? 24)
    const span = (run: Run) => (
      <Text
        color={run.color}
        backgroundColor={run.background}
        bold={run.bold}
        italic={run.italic}
        underline={run.underline}
        strikethrough={run.strikethrough}
        dimColor={run.dim}
      >
        {run.text}
      </Text>
    )

    return (
      <Box flexDirection="column">
        {visibleRows(frame.screen, room).map(row => (
          <Text wrap="truncate-end">{row.length > 0 ? row.map(span) : ' '}</Text>
        ))}
      </Box>
    )
  })
}
