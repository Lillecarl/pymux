/**
 * Whether a stream that closed opens again, and when.
 *
 * A module of its own so that `tests/retry.test.mjs` can run it under
 * node, as `keys.ts` is. Lillecarl/pymux#492.
 */

/** The close code the server sends when it will not serve a stream. */
export const REFUSED = 1008;

/**
 * How long to wait before each attempt, in milliseconds. About two and a
 * half minutes in all: `pymux web` makes a new token each time it starts,
 * so past a restart of it no attempt can succeed.
 */
export const RETRY_DELAYS = [
  500, 1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000, 30000,
];

/**
 * The wait before the next attempt, or `null` for none.
 *
 * - `welcomed`: whether a welcome came since the address was set. A
 *   stream that never reached the server has a wrong address or a wrong
 *   token, and the browser reports both as code 1006, the same as a lost
 *   network. Only a stream that once worked is tried again.
 * - `failures`: the attempts since the last welcome.
 *
 * Any other code is tried: the server closes with 1000 when its own
 * connection to pymux ends, which an upgrade does to a pane that stays.
 * A pane that is gone answers the attempt with `REFUSED`, which stops it.
 */
export function retryAfter(
  code: number,
  welcomed: boolean,
  failures: number,
): number | null {
  if (!welcomed || code === REFUSED) return null;
  return RETRY_DELAYS[failures] ?? null;
}
