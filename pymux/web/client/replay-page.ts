/**
 * The replay page's own script: pick a `.web.json`, and play it.
 *
 * `?src=` names a file this server serves; without one the page asks for
 * a file off the disk, which a browser reads without sending anywhere.
 * Lillecarl/pymux#610.
 */

import "./pymux-pane.js";
import { Player, replayOf } from "./replay.js";

function needed<T>(found: T | null, what: string): T {
  if (found === null) throw new Error(`the page has no ${what}`);
  return found;
}

const element = needed(document.querySelector("pymux-pane"), "pane");
const about = needed(document.getElementById("about"), "caption");
const picker = needed(document.querySelector<HTMLInputElement>("#file"), "file picker");
const toggle = needed(document.querySelector<HTMLButtonElement>("#toggle"), "play button");
const restart = needed(document.querySelector<HTMLButtonElement>("#restart"), "restart button");
const speed = needed(document.querySelector<HTMLSelectElement>("#speed"), "speed");

let player: Player | null = null;

function say(text: string): void {
  about.textContent = text;
}

function show(): void {
  toggle.disabled = restart.disabled = player === null;
  toggle.textContent = player?.playing ? "Pause" : "Play";
}

function load(text: string, name: string): void {
  player?.pause();
  try {
    player = new Player(element, replayOf(text));
  } catch (error) {
    player = null;
    say(`${name}: ${error instanceof Error ? error.message : error}`);
    show();
    return;
  }
  player.speed = Number(speed.value);
  for (const happened of ["play", "pause", "ended"]) {
    player.addEventListener(happened, show);
  }
  const { columns, rows } = player.size;
  say(`${name}: ${columns}x${rows}, ${player.duration.toFixed(1)}s`);
  player.restart();
  player.play();
}

picker.addEventListener("change", async () => {
  const file = picker.files?.[0];
  if (file) load(await file.text(), file.name);
});

toggle.addEventListener("click", () => (player?.playing ? player.pause() : player?.play()));
restart.addEventListener("click", () => {
  player?.restart();
  player?.play();
});
speed.addEventListener("change", () => {
  if (!player) return;
  // A new speed starts from where it is, so pause and play again.
  const playing = player.playing;
  player.pause();
  player.speed = Number(speed.value);
  if (playing) player.play();
});

const src = new URLSearchParams(location.search).get("src");
if (src) {
  fetch(src)
    .then((answer) => {
      if (!answer.ok) throw new Error(`${answer.status} ${answer.statusText}`);
      return answer.text();
    })
    .then((text) => load(text, src))
    .catch((error) => say(`${src}: ${error}`));
}
show();
