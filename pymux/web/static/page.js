/**
 * The demo page's own script.
 *
 * Separate from `pymux-pane.js` because that is the thing somebody
 * imports and this is the room around it. A page of their own replaces
 * this file and keeps the element.
 *
 * It is a module and not an inline script, because the server sends
 * `default-src 'self'` and an inline script would be blocked -- which is
 * the point: the element needs no loosening at all, so a page around it
 * need not ask for any.
 */

import "./pymux-pane.js";

const asked = new URLSearchParams(location.search);
const pane = asked.get("pane");
const token = asked.get("t");
const about = document.getElementById("about");
const element = document.getElementById("pane");

function say(text) {
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
