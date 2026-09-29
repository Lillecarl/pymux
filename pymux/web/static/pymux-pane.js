/**
 * `<pymux-pane>`: one pane of a pymux server, drawn in a browser.
 *
 * This is the example client, and it is meant to be read as well as run:
 * it is the only complete account of what a frame holds. The protocol is
 * Lillecarl/pymux#461, and `pymux/web/protocol.py` is the other half.
 *
 * **Nothing here knows what bold is.** A frame carries a style table --
 * the declarations for a way of drawing, sent once and referenced by a
 * number after that -- and this turns each entry into one CSS rule. So
 * `pyte.html` stays the only place a cell's CSS is written, and a row of
 * a hundred styled runs costs a hundred small integers on the wire.
 *
 * **No inline styles, no `eval`, no `data:` URLs.** Every rule goes
 * through the CSSOM into a constructed stylesheet, which a
 * Content-Security-Policy of `default-src 'self'` allows with nothing
 * added: CSP governs `<style>` elements and `style` attributes, not
 * stylesheets a script builds. So a page embedding this needs no
 * `unsafe-inline` of any kind.
 *
 *     <script type="module" src="/pymux-pane.js"></script>
 *     <pymux-pane src="ws://127.0.0.1:8080/pane/%1001?t=TOKEN"></pymux-pane>
 *
 * A page that opens its own socket -- so that a cookie and an `Origin`
 * stay the page's own -- assigns it instead:
 *
 *     document.querySelector('pymux-pane').socket = myWebSocket;
 */

/** The class every rule of the server's stylesheet is written under. */
const SCREEN = "pyte-screen";

/** What a run's class name is built from: `pyte-s7` for style 7. */
const STYLE_CLASS = "pyte-s";

/** The style number that needs no declarations at all. */
const PLAIN = 0;

/**
 * The element's own rules.
 *
 * They are about the box and the cursor. Everything about a cell comes
 * from the server, so nothing here says what a colour is.
 */
const OWN_RULES = `
:host {
  display: block;
  position: relative;
  font-family: monospace;
  line-height: 1.2;
  --pymux-cursor-row: -1;
  --pymux-cursor-column: -1;
}
:host([hidden]) { display: none; }
.${SCREEN} {
  margin: 0;
  outline: none;
}
/* The one focusable thing, and it is off screen rather than hidden:
   a composition needs a focused editable element to draw its preedit
   in, and one that is `display: none` is not focusable. */
.keyboard {
  position: absolute;
  left: -9999px;
  width: 1px;
  height: 1px;
  opacity: 0;
}
.cursor {
  position: absolute;
  width: 1ch;
  height: 1.2em;
  top: calc(var(--pymux-cursor-row) * 1.2em);
  left: calc(var(--pymux-cursor-column) * 1ch);
  background: currentColor;
  opacity: 0.6;
  pointer-events: none;
}
.cursor[hidden] { display: none; }
`;

/** Text a viewer typed, which the server writes as it stands. */
const TEXT = "text";

/** The names of keys, which the server spells. */
const INPUT = "input";

/** Text that becomes a bracketed paste when the program asked for one. */
const PASTE = "paste";

/**
 * What to send for a key that means more than the character it types.
 *
 * **Only the keys that have no text.** Anything that produces a
 * character is left to `beforeinput`, which is what an IME, a dead key,
 * an emoji picker and dictation all arrive through; a `keydown` handler
 * that also sent those would send them twice.
 *
 * The names are pymux's own, because the server spells them: it owns the
 * three keyboard modes and knows which the program asked for.
 */
const NAMED_KEYS = {
  Enter: "Enter",
  Tab: "Tab",
  Backspace: "BSpace",
  Delete: "DC",
  Escape: "Escape",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Home: "Home",
  End: "End",
  PageUp: "PageUp",
  PageDown: "PageDown",
  Insert: "IC",
  F1: "F1",
  F2: "F2",
  F3: "F3",
  F4: "F4",
  F5: "F5",
  F6: "F6",
  F7: "F7",
  F8: "F8",
  F9: "F9",
  F10: "F10",
  F11: "F11",
  F12: "F12",
};

/** `keyCode` a browser reports for a key an IME has taken. */
const TAKEN_BY_AN_IME = 229;

export class PymuxPane extends HTMLElement {
  static observedAttributes = ["src"];

  #screen;
  #keyboard;
  #cursor;
  #ownSheet;
  #themeSheet;
  #styleSheet;
  #styles = new Map();
  #rows = [];
  #socket = null;
  #ownsSocket = false;
  #writable = false;
  #columns = 0;

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });

    this.#ownSheet = new CSSStyleSheet();
    this.#ownSheet.replaceSync(OWN_RULES);
    // The server's stylesheet, which arrives in the welcome, and the
    // rules for the style table, which arrive as the table grows.
    this.#themeSheet = new CSSStyleSheet();
    this.#styleSheet = new CSSStyleSheet();
    root.adoptedStyleSheets = [this.#ownSheet, this.#themeSheet, this.#styleSheet];

    this.#screen = document.createElement("pre");
    this.#screen.className = SCREEN;
    this.#screen.tabIndex = 0;

    // A textarea and not the `pre`, because a composition draws its
    // preedit in a focused editable element. It holds nothing: every
    // `beforeinput` is cancelled once its text is on the wire.
    this.#keyboard = document.createElement("textarea");
    this.#keyboard.className = "keyboard";
    this.#keyboard.setAttribute("autocapitalize", "off");
    this.#keyboard.setAttribute("autocomplete", "off");
    this.#keyboard.setAttribute("spellcheck", "false");

    this.#cursor = document.createElement("div");
    this.#cursor.className = "cursor";
    this.#cursor.hidden = true;

    root.append(this.#screen, this.#keyboard, this.#cursor);

    this.#screen.addEventListener("mousedown", () => this.#keyboard.focus());
    this.#keyboard.addEventListener("keydown", (event) => this.#onKeyDown(event));
    this.#keyboard.addEventListener("beforeinput", (event) => this.#onBeforeInput(event));
    this.#keyboard.addEventListener("compositionend", (event) => {
      if (event.data) this.send({ type: TEXT, text: event.data });
      this.#keyboard.value = "";
    });
  }

  // -- the socket ----------------------------------------------------

  attributeChangedCallback(name, _was, now) {
    if (name === "src" && now) this.#open(now);
  }

  connectedCallback() {
    const src = this.getAttribute("src");
    if (src && !this.#socket) this.#open(src);
  }

  disconnectedCallback() {
    this.close();
  }

  /**
   * The socket to read frames from.
   *
   * Setting one hands this element a socket the page opened itself,
   * which is how a cookie and an `Origin` stay the page's own. The
   * element does not close a socket it was given.
   */
  set socket(socket) {
    this.close();
    this.#socket = socket;
    this.#ownsSocket = false;
    this.#listen();
  }

  get socket() {
    return this.#socket;
  }

  /** Whether the server said this stream takes input. */
  get writable() {
    return this.#writable;
  }

  #open(url) {
    this.close();
    this.#socket = new WebSocket(url);
    this.#ownsSocket = true;
    this.#listen();
  }

  #listen() {
    const socket = this.#socket;
    socket.addEventListener("message", (event) => this.#onMessage(event));
    socket.addEventListener("open", () =>
      this.dispatchEvent(new CustomEvent("connected")),
    );
    socket.addEventListener("close", (event) =>
      this.dispatchEvent(
        new CustomEvent("closed", {
          detail: { code: event.code, reason: event.reason },
        }),
      ),
    );
    socket.addEventListener("error", () =>
      this.dispatchEvent(new CustomEvent("error")),
    );
  }

  close() {
    if (this.#socket && this.#ownsSocket) this.#socket.close();
    this.#socket = null;
  }

  /** Send one message as it stands. */
  send(message) {
    if (this.#socket && this.#socket.readyState === WebSocket.OPEN) {
      this.#socket.send(JSON.stringify(message));
    }
  }

  // -- what arrives --------------------------------------------------

  #onMessage(event) {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      this.dispatchEvent(new CustomEvent("error", { detail: "bad frame" }));
      return;
    }

    if (frame.type === "welcome") {
      this.#writable = Boolean(frame.writable);
      this.#themeSheet.replaceSync(frame.css || "");
      this.#resize(frame.size);
      return;
    }
    if (frame.type !== "frame") return;

    if (frame.whole) {
      // Every row the element holds was drawn under another size,
      // another reverse video or another palette, so none of it counts.
      if (frame.css) this.#themeSheet.replaceSync(frame.css);
      this.#resize(frame.size);
    }
    if (frame.styles) this.#learnStyles(frame.styles);
    for (const [number, runs] of Object.entries(frame.rows || {})) {
      this.#drawRow(Number(number), runs);
    }
    if (frame.cursor) this.#moveCursor(frame.cursor);
  }

  #learnStyles(styles) {
    for (const [number, entry] of Object.entries(styles)) {
      if (this.#styles.has(number)) continue;
      this.#styles.set(number, entry);
      // One rule per way of drawing, inserted once. The declarations are
      // the server's; nothing here knows what they mean.
      if (entry.s) {
        this.#styleSheet.insertRule(
          `.${STYLE_CLASS}${number} { ${entry.s} }`,
          this.#styleSheet.cssRules.length,
        );
      }
    }
  }

  #resize(size) {
    if (!size) return;
    this.#columns = size.columns;
    this.#screen.textContent = "";
    this.#rows = [];
    for (let index = 0; index < size.rows; index += 1) {
      const row = document.createElement("div");
      this.#rows.push(row);
      this.#screen.append(row);
    }
  }

  #drawRow(number, runs) {
    const row = this.#rows[number];
    if (!row) return;
    row.textContent = "";

    for (const [style, text] of runs) {
      const entry = this.#styles.get(String(style));
      if (style === PLAIN && !entry) {
        // **`textContent` and never markup.** A program in the pane
        // writes every character of this, so building a string and
        // assigning `innerHTML` is where a `<script>` a program wrote
        // would become one.
        row.append(document.createTextNode(text));
        continue;
      }
      const piece =
        entry && entry.h
          ? document.createElement("a")
          : document.createElement("span");
      if (entry && entry.h) {
        piece.href = entry.h;
        piece.rel = "noreferrer noopener";
        piece.target = "_blank";
      }
      if (entry && entry.s) piece.className = `${STYLE_CLASS}${style}`;
      piece.textContent = text;
      row.append(piece);
    }

    // A row with nothing in it still takes a line, the way a blank row
    // of a terminal does.
    if (!row.firstChild) row.append(document.createTextNode(" "));
  }

  #moveCursor(cursor) {
    const off = cursor.row < 0;
    this.#cursor.hidden = off;
    if (off) return;
    const style = this.#ownSheet.cssRules[0].style;
    style.setProperty("--pymux-cursor-row", String(cursor.row));
    style.setProperty("--pymux-cursor-column", String(cursor.column));
  }

  // -- what a viewer types -------------------------------------------

  #onKeyDown(event) {
    // **Nothing while a composition runs.** A dead key and an IME both
    // deliver their keystrokes here as well, and the committed text
    // arrives separately; sending both would type it twice.
    if (event.isComposing || event.keyCode === TAKEN_BY_AN_IME) return;

    const named = NAMED_KEYS[event.key];
    const modified = event.ctrlKey || event.altKey || event.metaKey;

    if (!named && !modified) return; // `beforeinput` carries the text.

    event.preventDefault();

    const parts = [];
    if (event.ctrlKey) parts.push("C");
    if (event.altKey) parts.push("M");
    if (event.shiftKey && named) parts.push("S");
    parts.push(named || event.key);
    this.send({ type: INPUT, keys: parts.join("-") });
  }

  #onBeforeInput(event) {
    if (event.inputType === "insertFromPaste") {
      event.preventDefault();
      const text = event.data ?? event.dataTransfer?.getData("text") ?? "";
      if (text) this.send({ type: PASTE, text });
      return;
    }
    if (event.inputType === "insertText" && event.data !== null) {
      event.preventDefault();
      this.send({ type: TEXT, text: event.data });
      return;
    }
    if (event.inputType === "insertLineBreak") {
      event.preventDefault();
      this.send({ type: INPUT, keys: "Enter" });
    }
  }
}

customElements.define("pymux-pane", PymuxPane);
