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
const pane = asked.get("pane");
const token = asked.get("t");
// `querySelector` and not `getElementById`, because the tag map makes
// this a `PymuxPane` where `getElementById` gives a bare `HTMLElement`:
// the line below reads `writable` off it.
const element = needed(document.querySelector("pymux-pane"), "pane");
const about = needed(document.getElementById("about"), "caption");

function say(text: string): void {
  about.textContent = text;
}

if (!pane || !token) {
  say("Open the address that `pymux web` printed, with ?pane=%1001 on it.");
} else {
  const where = new URL(location.href);
  where.protocol = where.protocol === "https:" ? "wss:" : "ws:";
  where.pathname = `/pane/${pane}`;
  where.search = `?t=${encodeURIComponent(token)}`;

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
  element.addEventListener("error", () => say(`${pane}: the stream failed.`));

  element.setAttribute("src", where.toString());
}
