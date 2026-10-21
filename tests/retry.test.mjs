/**
 * When `<pymux-pane>` opens a closed stream again. Reads the compiled
 * `retry.js` out of `pymux/web/static/`, as `keys.test.mjs` does.
 * Lillecarl/pymux#492.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

import { REFUSED, RETRY_DELAYS, retryAfter } from "../pymux/web/static/retry.js";

const LOST = 1006;
const ENDED = 1000;

test("a lost stream that once worked is tried again, slower each time", () => {
  assert.equal(retryAfter(LOST, true, 0), RETRY_DELAYS[0]);
  assert.ok(retryAfter(LOST, true, 3) > retryAfter(LOST, true, 2));
});

test("a stream the server ended is tried again", () => {
  assert.equal(retryAfter(ENDED, true, 0), RETRY_DELAYS[0]);
});

test("a stream that never reached the server is not tried again", () => {
  assert.equal(retryAfter(LOST, false, 0), null);
});

test("a stream the server refused is not tried again", () => {
  assert.equal(retryAfter(REFUSED, true, 0), null);
});

test("the attempts end", () => {
  assert.equal(retryAfter(LOST, true, RETRY_DELAYS.length), null);
});
