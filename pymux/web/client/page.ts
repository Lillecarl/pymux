/**
 * The demo page's own script.
 *
 * Separate from `pymux-pane.ts` because that is the thing somebody
 * imports and this is the room around it. A page of their own replaces
 * this file and keeps the element.
 *
 * It is a module and not an inline script, because the server sends
 * `default-src 'self'` and an inline script would be blocked -- which is
 * the point: the element needs no loosening at all, so a page around it
 * need not ask for any.
 */

// `.js` and not `.ts`, because this line survives into the emitted
// `page.js` and a browser resolves it. The compiler reads it as the
// TypeScript beside this file.
import type { Modifier } from "./pymux-pane.js";
import "./pymux-pane.js";

/** One element of this page's own markup, which ships beside it. */
function needed<T>(found: T | null, what: string): T {
  if (found === null) throw new Error(`the page has no ${what}`);
  return found;
}

const asked = new URLSearchParams(location.search);
// The whole session as one client draws it, with the status line and
// whatever pymux draws over the panes, unless `?pane=%1` asks for one
// pane's own screen. Lillecarl/pyterm#483.
const session = asked.has("session") || !asked.has("pane");
const pane = session ? "the session" : asked.get("pane");
const token = asked.get("t");
// `querySelector` and not `getElementById`, because the tag map makes
// this a `PymuxPane` where `getElementById` gives a bare `HTMLElement`:
// the line below reads `writable` off it.
const element = needed(document.querySelector("pymux-pane"), "pane");
const about = needed(document.getElementById("about"), "caption");
const fullscreen = needed(document.getElementById("fullscreen"), "full screen button");

// The click is the gesture full screen needs. Lillecarl/pyterm#953.
fullscreen.addEventListener("click", () => {
  const refused = (error: unknown) => say(`Full screen was refused: ${String(error)}`);
  // With the key row shown, the whole page fills the screen: the pane
  // alone would leave a phone without Esc and Ctrl.
  if (getComputedStyle(row).display !== "none") {
    void document.documentElement.requestFullscreen().catch(refused);
  } else {
    void element.fullscreen().catch(refused);
  }
});

// **A browser with no Keyboard Lock keeps Escape in full screen**:
// Firefox and Safari leave full screen on it, and the page never hears
// it. Escape is pymux's abort, so such a browser gets Ctrl-[, which a
// terminal sends as the same byte, and a button.
const escape = needed(document.getElementById("escape"), "escape button");
const hint = needed(document.getElementById("escape-hint"), "escape hint");
escape.hidden = hint.hidden = "keyboard" in navigator;
// The mousedown would move the focus to the button, and the next key
// would go nowhere.
escape.addEventListener("mousedown", (event) => event.preventDefault());
escape.addEventListener("click", () => element.send({ type: "input", keys: "Escape" }));

// The key row. `?keys` shows it on a page with a mouse too.
// Lillecarl/pyterm#1051.
const row = needed(document.getElementById("keys"), "key row");
if (asked.has("keys")) document.body.classList.add("keys");
// A tap must leave the focus, and so the on-screen keyboard, where it is.
row.addEventListener("pointerdown", (event) => event.preventDefault());
row.addEventListener("mousedown", (event) => event.preventDefault());
row.addEventListener("click", (event) => {
  const button = (event.target as Element).closest("button");
  if (!button) return;
  const { key, hold } = button.dataset;
  if (key) element.press(key);
  if (hold === "ctrl" || hold === "alt") element.hold(hold);
});
element.addEventListener("held", (event) => {
  for (const button of row.querySelectorAll<HTMLButtonElement>("[data-hold]")) {
    button.setAttribute("aria-pressed", String(event.detail.includes(button.dataset.hold as Modifier)));
  }
});

// `?probe` lists every keyboard, input and composition event the
// element's keyboard gets: what an on-screen keyboard really sends, read
// on the phone. Each of these events crosses the shadow root.
// Lillecarl/pyterm#1052.
if (asked.has("probe")) {
  const log = document.createElement("pre");
  log.id = "probe";
  document.body.append(log);
  const kinds = ["keydown", "keyup", "beforeinput", "input", "compositionstart", "compositionupdate", "compositionend"];
  for (const kind of kinds) {
    element.addEventListener(kind, (event) => {
      const seen: Record<string, unknown> = { type: event.type };
      if (event instanceof KeyboardEvent) {
        Object.assign(seen, { key: event.key, code: event.code, keyCode: event.keyCode, isComposing: event.isComposing });
      } else if (event instanceof InputEvent) {
        Object.assign(seen, { inputType: event.inputType, data: event.data, isComposing: event.isComposing });
      } else if (event instanceof CompositionEvent) {
        Object.assign(seen, { data: event.data });
      }
      // What the hidden textarea holds: an IME reads it back, and a
      // value it did not expect is where it repeats a word.
      const target = event.composedPath()[0];
      if (target instanceof HTMLTextAreaElement) seen.value = target.value;
      log.textContent = `${JSON.stringify(seen)}\n${log.textContent ?? ""}`;
      // And to `pymux web`, which prints it: a phone's screen is hard to
      // read from anywhere else. Lillecarl/pyterm#1069.
      element.send({ type: "probe", event: seen });
    });
  }
}

function say(text: string): void {
  about.textContent = text;
}

/** Where the page keeps the client options of a session view. */
const KEPT_OPTIONS = "pymux-client-options";

// A private window, or a browser that blocks storage, throws on either.
function keptOptions(): string | null {
  try {
    return localStorage.getItem(KEPT_OPTIONS);
  } catch {
    return null;
  }
}

function keepOptions(options: string): void {
  try {
    localStorage.setItem(KEPT_OPTIONS, options);
  } catch {
    // Kept for this stream's reconnects only, by the element.
  }
}

if (!pane || !token) {
  say(
    "Open the address that `pymux web` printed: it carries the token.",
  );
} else {
  const where = new URL(location.href);
  where.protocol = where.protocol === "https:" ? "wss:" : "ws:";
  where.pathname = session ? "/session" : `/pane/${pane}`;
  where.search = `?t=${encodeURIComponent(token)}`;
  if (session) {
    // The element says how many cells it holds, and the page gives it
    // the room: a client's size is its terminal's.
    element.setAttribute("fit", "");
    document.body.classList.add("session");
    // What the person set on this view's client, kept for the next one:
    // each stream is a new client. Lillecarl/pyterm#959.
    const kept = keptOptions();
    if (kept !== null) where.searchParams.set("options", kept);
    element.addEventListener("client-options", (event) => keepOptions(JSON.stringify(event.detail)));
  }

  element.addEventListener("connected", () =>
    say(
      element.writable
        ? `${pane}: click it and type.`
        : `${pane}: showing only. \`pymux web --allow-input\` takes keys.`,
    ),
  );
  element.addEventListener("closed", (event) =>
    say(`${pane}: the stream ended (${event.detail.code}).`),
  );
  element.addEventListener("reconnecting", (event) =>
    say(
      `${pane}: the stream ended; trying again in ${Math.ceil(event.detail.delay / 1000)} s.`,
    ),
  );
  element.addEventListener("error", () => say(`${pane}: the stream failed.`));

  element.setAttribute("src", where.toString());
}
