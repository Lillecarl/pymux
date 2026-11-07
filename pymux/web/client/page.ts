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
import "./pymux-pane.js";

/** One element of this page's own markup, which ships beside it. */
function needed<T>(found: T | null, what: string): T {
  if (found === null) throw new Error(`the page has no ${what}`);
  return found;
}

const asked = new URLSearchParams(location.search);
// `?session` shows the whole session as one client draws it, with the
// status line and whatever pymux draws over the panes; `?pane=%1` shows
// one pane's own screen. Lillecarl/pyterm#483.
const session = asked.has("session");
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
  void element.fullscreen().catch((error: unknown) => say(`Full screen was refused: ${String(error)}`));
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
    "Open the address that `pymux web` printed, with ?pane=%1001 or ?session on it.",
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
