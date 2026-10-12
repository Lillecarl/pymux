/**
 * The player of `record-export -F web`, run on the compiled `replay.js`.
 *
 * The element needs a browser, and the player needs only something with
 * `receive`, so a list stands in for it. Lillecarl/pymux#610.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

import { Player, replayOf } from "../pymux/web/static/replay.js";

const WELCOME = { type: "welcome", revision: 0, writable: false, css: "", size: { columns: 10, rows: 2 } };

function frame(revision) {
  return { type: "frame", revision, rows: {}, cursor: { row: 0, column: 0 } };
}

function aReplay(times) {
  return {
    format: "pymux-replay",
    version: 1,
    lines: 2,
    columns: 10,
    events: times.map((seconds, n) => [seconds, n === 0 ? WELCOME : frame(n)]),
  };
}

function watching() {
  const received = [];
  return { received, element: { receive: (message) => received.push([performance.now(), message.revision]) } };
}

function ended(player) {
  return new Promise((resolve) => player.addEventListener("ended", resolve, { once: true }));
}

test("every message arrives once, in order, at its time", async () => {
  const { received, element } = watching();
  const player = new Player(element, aReplay([0, 0.05, 0.15, 0.3]));
  const started = performance.now();
  player.play();
  await ended(player);
  assert.deepEqual(
    received.map(([, revision]) => revision),
    [0, 1, 2, 3],
  );
  // Never early. Late by a timer's slack at most, and the times are
  // from the start, so the slack does not add up.
  const offsets = received.map(([at]) => (at - started) / 1000);
  [0, 0.05, 0.15, 0.3].forEach((due, n) => {
    assert.ok(offsets[n] >= due - 0.005, `message ${n} came at ${offsets[n]}, before ${due}`);
    assert.ok(offsets[n] < due + 0.1, `message ${n} came at ${offsets[n]}, long after ${due}`);
  });
});

test("a pause holds the next message back, and play goes on from there", async () => {
  const { received, element } = watching();
  const player = new Player(element, aReplay([0, 0.1, 0.2]));
  player.play();
  player.pause();
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.deepEqual(
    received.map(([, revision]) => revision),
    [0],
  );
  player.play();
  await ended(player);
  assert.deepEqual(
    received.map(([, revision]) => revision),
    [0, 1, 2],
  );
});

test("a faster speed takes less time", async () => {
  const { element } = watching();
  const player = new Player(element, aReplay([0, 0.4]));
  player.speed = 4;
  const started = performance.now();
  player.play();
  await ended(player);
  assert.ok(performance.now() - started < 300);
});

test("a file that is not a replay is refused by name", () => {
  assert.throws(() => replayOf('{"version": 2}'), /not a pymux replay/);
  assert.throws(() => replayOf(JSON.stringify({ ...aReplay([]), events: [] })), /holds nothing/);
});
