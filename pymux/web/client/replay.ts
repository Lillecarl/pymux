/**
 * A player for `record-export -F web`: the frames of a recording, handed
 * to `<pymux-pane>` at the times they came. Lillecarl/pymux#610.
 *
 * It takes a parsed file and an element, and nothing about where either
 * came from, so a page that fetched the file, a page that read one off
 * the disk and a file with the replay inside it are three callers of
 * this one thing. `pymux/web/replay.py` is the other half.
 *
 * The idle limit is already in the times: the export applied it.
 */

import type { Frame, PymuxPane, Size, Welcome } from "./pymux-pane.js";

/** What `record-export -F web` writes. */
export interface Replay {
  format: "pymux-replay";
  version: 1;
  lines: number;
  columns: number;
  term?: string;
  /** When the recording started, in seconds since the epoch. */
  timestamp?: number;
  /** The longest pause left in the times, in seconds. */
  idle_time_limit?: number;
  /** Seconds from the start, and a message for the element. */
  events: [seconds: number, message: Welcome | Frame][];
}

/** A replay out of a file's text, or an error saying why it is not one. */
export function replayOf(text: string): Replay {
  const replay = JSON.parse(text) as Partial<Replay>;
  if (replay.format !== "pymux-replay" || replay.version !== 1) {
    throw new Error("not a pymux replay: export one with record-export -F web");
  }
  if (!Array.isArray(replay.events) || replay.events.length === 0) {
    throw new Error("the replay holds nothing");
  }
  return replay as Replay;
}

/** Plays one replay on one element, from the start, at a speed. */
export class Player extends EventTarget {
  readonly replay: Replay;
  readonly element: PymuxPane;
  /** How many times faster than it was recorded. */
  speed = 1;

  /** The next event to hand over. */
  #next = 0;
  /** Where in the recording a pause left it, in seconds. */
  #position = 0;
  /** What `performance.now()` read at the recording's second 0. */
  #origin = 0;
  #timer: ReturnType<typeof setTimeout> | null = null;

  constructor(element: PymuxPane, replay: Replay) {
    super();
    this.element = element;
    this.replay = replay;
  }

  get playing(): boolean {
    return this.#timer !== null;
  }

  /** How long the whole replay runs, in seconds of the recording. */
  get duration(): number {
    const events = this.replay.events;
    return events[events.length - 1][0];
  }

  /** Where it is now, in seconds of the recording. */
  get position(): number {
    if (!this.playing) return this.#position;
    return ((performance.now() - this.#origin) * this.speed) / 1000;
  }

  /** The size the recording started at. */
  get size(): Size {
    return { columns: this.replay.columns, rows: this.replay.lines };
  }

  play(): void {
    if (this.playing) return;
    if (this.#next >= this.replay.events.length) this.restart();
    this.#origin = performance.now() - (this.#position * 1000) / this.speed;
    this.#schedule();
    this.dispatchEvent(new Event("play"));
  }

  pause(): void {
    if (!this.playing) return;
    this.#position = this.position;
    clearTimeout(this.#timer!);
    this.#timer = null;
    this.dispatchEvent(new Event("pause"));
  }

  /** Back to the first event, drawn at once, and paused. */
  restart(): void {
    this.pause();
    this.#next = 0;
    this.#position = 0;
    this.#handOver(0);
  }

  /** Hand over every event that is due, then wait for the next one. */
  #schedule(): void {
    const events = this.replay.events;
    this.#handOver(this.position);
    if (this.#next >= events.length) {
      this.#position = this.duration;
      this.#timer = null;
      this.dispatchEvent(new Event("ended"));
      return;
    }
    // From the origin, and not from the last event: a timer fires late,
    // and a delay measured from when it fired would add every lateness up.
    const due = this.#origin + (events[this.#next][0] * 1000) / this.speed;
    this.#timer = setTimeout(() => this.#schedule(), Math.max(0, due - performance.now()));
  }

  #handOver(until: number): void {
    const events = this.replay.events;
    while (this.#next < events.length && events[this.#next][0] <= until) {
      this.element.receive(events[this.#next][1]);
      this.#next++;
    }
  }
}
