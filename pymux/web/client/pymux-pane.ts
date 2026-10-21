/**
 * `<pymux-pane>`: one pane of a pymux server, drawn in a browser.
 *
 * This is the example client, and it is meant to be read as well as run:
 * it is the only complete account of what a frame holds. The protocol is
 * Lillecarl/pymux#461, and `pymux/web/protocol.py` is the other half.
 *
 * **The types here are the published ones.** `tsc` emits
 * `pymux-pane.js` and `pymux-pane.d.ts` from this file, so what a
 * consumer's compiler reads is derived from what runs rather than
 * written beside it. `nix/element.nix` is that build.
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

// `.js` and not `.ts`: this line survives into the emitted
// `pymux-pane.js`, where a browser resolves it.
import { keysFor } from "./keys.js";
import { retryAfter } from "./retry.js";

// ----------------------------------------------------------------------
// The protocol, as a consumer reads it.

/** One run of a row: the number of a way of drawing, and its characters. */
export type Run = [style: number, text: string];

/** What a style number means, sent once and referenced by number after. */
export interface StyleEntry {
  /** The CSS declarations for a cell drawn this way. */
  s?: string;
  /**
   * The classes of the server's stylesheet that draw this cell.
   *
   * Most of a rendition carries no value -- bold, the lines, the
   * underline shapes, the themed colours -- so the server names a rule
   * it already serves rather than sending the declarations again.
   * Lillecarl/pymux#460.
   */
  c?: string;
  /** The `href` of a link the program opened, already allowlisted. */
  h?: string;
}

export interface Size {
  columns: number;
  rows: number;
}

/** Where the cursor is, in rows of the screen. `-1` is off the screen. */
export interface Cursor {
  row: number;
  column: number;
}

/** The first message of a stream. */
export interface Welcome {
  type: "welcome";
  revision: number;
  /** Whether this stream takes input. The server enforces it. */
  writable: boolean;
  /**
   * The whole stylesheet the runs are written against.
   *
   * A client has no second route to the server, so it travels with the
   * stream. `Frame.palette` is a different and smaller thing.
   */
  css: string;
  size: Size;
}

/** The rows that changed, and nothing else. */
export interface Frame {
  type: "frame";
  revision: number;
  /** Keyed by row of the screen. A row that is absent is unchanged. */
  rows: Record<string, Run[]>;
  cursor: Cursor;
  /** Ways of drawing this stream has not sent before. */
  styles?: Record<string, StyleEntry>;
  /** Everything changed: throw away every row held. */
  whole?: true;
  size?: Size;
  reverse?: boolean;
  /**
   * The sixteen colours and the two defaults, when a program changed one.
   *
   * **Not the stylesheet.** `Welcome.css` is that, and this is only the
   * custom properties it defines. They are named apart because a client
   * that put both into one stylesheet lost every rule the welcome sent.
   */
  palette?: string;
  /**
   * Images this stream has not sent before, by number: a PNG, in base64.
   * Lillecarl/pymux#480.
   */
  images?: Record<string, string>;
  /**
   * Every image the screen shows, when that changed. An image no
   * placement names is one the server has forgotten too.
   */
  placements?: Placement[];
}

/**
 * One image on the screen: the cells it covers, then the rectangle of
 * the image's pixels that fills them.
 */
export type Placement = [
  image: number,
  row: number,
  column: number,
  rows: number,
  columns: number,
  x: number,
  y: number,
  width: number,
  height: number,
];

/** The names of keys, which the server spells. */
export interface InputMessage {
  type: "input";
  keys: string;
}

/** Characters a viewer finished composing, written as typed. */
export interface TextMessage {
  type: "text";
  text: string;
}

/** Text that becomes a bracketed paste when the program asked for one. */
export interface PasteMessage {
  type: "paste";
  text: string;
}

/**
 * How many cells the element holds. Only `/session` reads it: a pane has
 * a size of its own, and a client's size is its terminal's.
 * Lillecarl/pymux#481.
 */
export interface SizeMessage {
  type: "size";
  rows: number;
  columns: number;
}

export type ViewerMessage =
  | InputMessage
  | TextMessage
  | PasteMessage
  | SizeMessage;

export interface ClosedDetail {
  code: number;
  reason: string;
}

/** The element opens its stream again after `delay` milliseconds. */
export interface ReconnectingDetail {
  delay: number;
  /** One for the first attempt since the stream last worked. */
  attempt: number;
}

// ----------------------------------------------------------------------
// The element.

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
  font-family: monospace;
  line-height: 1.2;
  --pymux-cursor-row: -1;
  --pymux-cursor-column: -1;
}
:host([hidden]) { display: none; }
/* The screen is what the cursor is measured against, so it is what
   carries the positioning. The host cannot: a page that gives the host
   padding then moves the cursor by that much, and 8px of padding put it
   one row up and one cell left of the cell it marks. Measured in a
   browser -- the offset was constant at columns 0, 2 and 199, which is
   what a padding box looks like and not what a font does. */
.${SCREEN} {
  position: relative;
  margin: 0;
  outline: none;
}
/* The one focusable thing, and it is off screen rather than hidden:
   a composition needs a focused editable element to draw its preedit
   in, and one that is "display: none" is not focusable.

   No backticks in here: this comment is inside a template literal, and
   one would close it. The compiler fails on that now, which is why this
   is a note about reading the file and not a warning about shipping it. */
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
/* Over the text, as kitty draws an image, and never in a click's way.
   Each canvas is placed by a rule of its own in the image sheet. */
.images {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.images canvas { position: absolute; }
/* What a cell measures, for "fit". Absolute, so it adds no row. */
.probe {
  position: absolute;
  visibility: hidden;
}
`;

/** Text a viewer typed, which the server writes as it stands. */
const TEXT = "text";

/** The names of keys, which the server spells. */
const INPUT = "input";

/** Text that becomes a bracketed paste when the program asked for one. */
const PASTE = "paste";

/**
 * The three events this dispatches, typed.
 *
 * An interface beside the class and not methods in it: overloads in a
 * class body need an implementation, and there is nothing to implement
 * -- `EventTarget` already carries the one that runs. Merging declares
 * them on the instance type, and `tsc` emits them into the `.d.ts`.
 */
export interface PymuxPane {
  /** The stream ended, with the code and reason the server closed on. */
  addEventListener(
    type: "closed",
    listener: (event: CustomEvent<ClosedDetail>) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
  /**
   * A stream this element opened, which once worked, closed and opens
   * again. Fires after `closed`. Lillecarl/pymux#492.
   */
  addEventListener(
    type: "reconnecting",
    listener: (event: CustomEvent<ReconnectingDetail>) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
  /**
   * `connected` is the welcome and not the socket: `writable` and the
   * size are true by then.
   */
  addEventListener(
    type: "connected" | "error",
    listener: (event: CustomEvent) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: boolean | AddEventListenerOptions,
  ): void;
}

/**
 * A PNG the server sent, as something a canvas draws. Not an `<img>`: a
 * `data:` or `blob:` address is one the page's `default-src 'self'`
 * refuses, and decoding bytes fetches nothing.
 */
async function decoded(png: string): Promise<ImageBitmap | null> {
  const bytes = Uint8Array.from(atob(png), (character) => character.charCodeAt(0));
  try {
    return await createImageBitmap(new Blob([bytes], { type: "image/png" }));
  } catch {
    return null;
  }
}

export class PymuxPane extends HTMLElement {
  static observedAttributes = ["src"];

  /**
   * With `fit`, the element tells the server how many cells its box
   * holds, at the welcome and whenever the box changes. That is what a
   * `/session` stream sizes its client by. A pane stream ignores it.
   */
  #observer = new ResizeObserver(() => this.#tellTheSize());
  #toldSize = "";

  #screen: HTMLPreElement;
  #keyboard: HTMLTextAreaElement;
  #cursor: HTMLDivElement;
  #ownSheet: CSSStyleSheet;
  #themeSheet: CSSStyleSheet;
  #paletteSheet: CSSStyleSheet;
  #styleSheet: CSSStyleSheet;
  #imageSheet: CSSStyleSheet;
  #images: HTMLDivElement;
  #bitmaps = new Map<string, Promise<ImageBitmap | null>>();
  #styles = new Map<string, StyleEntry>();
  #rows: HTMLDivElement[] = [];
  #socket: WebSocket | null = null;
  #ownsSocket = false;
  #writable = false;
  /** Whether a welcome came since `src` was set; see `retryAfter`. */
  #welcomed = false;
  #failures = 0;
  #retry: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    super();
    // `delegatesFocus`, so that focus arriving at the host -- a click, a
    // tab, a `focus()` from a page -- lands on the first focusable thing
    // in here, which is the textarea a composition needs.
    const root = this.attachShadow({ mode: "open", delegatesFocus: true });

    this.#ownSheet = new CSSStyleSheet();
    this.#ownSheet.replaceSync(OWN_RULES);
    // Three sheets from the server, and they are three on purpose.
    //
    // `themeSheet` is the whole stylesheet, which arrives once in the
    // welcome. `paletteSheet` is the sixteen colours and the two
    // defaults, which a `whole` frame sends again when a program changes
    // one; it comes after so that its properties win, and it is separate
    // so that it cannot replace the rules. `styleSheet` grows one rule
    // per way of drawing as the style table does.
    this.#themeSheet = new CSSStyleSheet();
    this.#paletteSheet = new CSSStyleSheet();
    this.#styleSheet = new CSSStyleSheet();
    this.#imageSheet = new CSSStyleSheet();
    root.adoptedStyleSheets = [
      this.#ownSheet,
      this.#themeSheet,
      this.#paletteSheet,
      this.#styleSheet,
      this.#imageSheet,
    ];

    this.#screen = document.createElement("pre");
    this.#screen.className = SCREEN;
    // **No `tabIndex` here.** With one, a click landed the keyboard on
    // the `pre` rather than on the textarea below and every key was
    // lost: the mousedown handler focuses the textarea and the click's
    // own default focus then takes it away again. Measured in a browser
    // -- `shadowRoot.activeElement` was the `pre`. The textarea is the
    // only focusable thing, which is also what makes `delegatesFocus`
    // land on it.

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

    this.#images = document.createElement("div");
    this.#images.className = "images";

    // The cursor goes **inside** the screen, so that it is positioned
    // against the screen's content box rather than the host's padding
    // box. A page that gives the host padding then moves the text and
    // the cursor together.
    this.#screen.append(this.#cursor, this.#images);
    root.append(this.#keyboard, this.#screen);

    this.#screen.addEventListener("mousedown", (event) => {
      // **`preventDefault`, or the click takes the focus back.** The
      // default action of a mousedown moves focus to whatever was
      // clicked, and a selection drag should not move the keyboard
      // either.
      event.preventDefault();
      this.#keyboard.focus();
    });
    this.#keyboard.addEventListener("keydown", (event) =>
      this.#onKeyDown(event),
    );
    this.#keyboard.addEventListener("beforeinput", (event) =>
      this.#onBeforeInput(event),
    );
    this.#keyboard.addEventListener("compositionend", (event) => {
      if (event.data) this.send({ type: TEXT, text: event.data });
      this.#keyboard.value = "";
    });
  }

  // -- the socket ----------------------------------------------------

  attributeChangedCallback(
    name: string,
    _was: string | null,
    now: string | null,
  ): void {
    if (name === "src" && now) this.#open(now);
  }

  connectedCallback(): void {
    const src = this.getAttribute("src");
    if (src && !this.#socket) this.#open(src);
    this.#observer.observe(this);
  }

  disconnectedCallback(): void {
    this.#observer.disconnect();
    this.close();
  }

  /**
   * The cells the box holds, measured with the glyph and the line height
   * the screen is drawn with, so the count agrees with the cursor's
   * `1ch` and `1.2em`.
   */
  #cellsThatFit(): Size | null {
    const probe = document.createElement("span");
    probe.textContent = "0".repeat(10);
    probe.className = "probe";
    this.#screen.append(probe);
    const glyph = probe.getBoundingClientRect();
    probe.remove();

    const lineHeight = glyph.height;
    const cellWidth = glyph.width / 10;
    if (!lineHeight || !cellWidth) return null;
    // The screen is a block, so its width is the host's content box. The
    // host's own height holds its padding as well, which a page may set.
    const host = getComputedStyle(this);
    const height =
      this.clientHeight -
      parseFloat(host.paddingTop) -
      parseFloat(host.paddingBottom);
    return {
      columns: Math.floor(this.#screen.clientWidth / cellWidth),
      rows: Math.floor(height / lineHeight),
    };
  }

  #tellTheSize(): void {
    if (!this.hasAttribute("fit")) return;
    const size = this.#cellsThatFit();
    if (!size || size.rows < 1 || size.columns < 1) return;
    const said = `${size.rows}x${size.columns}`;
    if (said === this.#toldSize) return;
    this.#toldSize = said;
    this.send({ type: "size", rows: size.rows, columns: size.columns });
  }

  /**
   * The socket to read frames from.
   *
   * Setting one hands this element a socket the page opened itself,
   * which is how a cookie and an `Origin` stay the page's own. The
   * element does not close a socket it was given.
   */
  set socket(socket: WebSocket | null) {
    this.close();
    this.#socket = socket;
    this.#ownsSocket = false;
    if (socket) this.#listen(socket);
  }

  get socket(): WebSocket | null {
    return this.#socket;
  }

  /** Whether the server said this stream takes input. */
  get writable(): boolean {
    return this.#writable;
  }

  #open(url: string): void {
    this.#welcomed = false;
    this.#failures = 0;
    this.#connect(url);
  }

  #connect(url: string): void {
    this.close();
    const socket = new WebSocket(url);
    this.#socket = socket;
    this.#ownsSocket = true;
    this.#listen(socket);
  }

  #listen(socket: WebSocket): void {
    socket.addEventListener("message", (event) => this.#onMessage(event));
    // **No `connected` on `open`.** A socket that is open has told a
    // page nothing yet: `writable` and the size arrive in the welcome, so
    // a listener that read them there read `false` and said "showing
    // only" to somebody who could type. Measured in a browser. The event
    // fires from the welcome instead, where what it reports is true.
    socket.addEventListener("close", (event) => {
      this.dispatchEvent(
        new CustomEvent<ClosedDetail>("closed", {
          detail: { code: event.code, reason: event.reason },
        }),
      );
      // A socket `close()` let go of, or one the page gave, stays closed.
      if (socket === this.#socket && this.#ownsSocket) this.#tryAgain(event.code);
    });
    socket.addEventListener("error", () =>
      this.dispatchEvent(new CustomEvent("error")),
    );
  }

  #tryAgain(code: number): void {
    const url = this.#socket?.url;
    const delay = retryAfter(code, this.#welcomed, this.#failures);
    if (url === undefined || delay === null) return;
    this.#failures += 1;
    this.#retry = setTimeout(() => this.#connect(url), delay);
    this.dispatchEvent(
      new CustomEvent<ReconnectingDetail>("reconnecting", {
        detail: { delay, attempt: this.#failures },
      }),
    );
  }

  /** Close a socket this element opened, and attempt nothing more. */
  close(): void {
    if (this.#retry !== null) clearTimeout(this.#retry);
    this.#retry = null;
    if (this.#socket && this.#ownsSocket) this.#socket.close();
    this.#socket = null;
  }

  /** Send one message as it stands. */
  send(message: ViewerMessage): void {
    if (this.#socket && this.#socket.readyState === WebSocket.OPEN) {
      this.#socket.send(JSON.stringify(message));
    }
  }

  // -- what arrives --------------------------------------------------

  #onMessage(event: MessageEvent): void {
    let frame: Welcome | Frame;
    try {
      frame = JSON.parse(String(event.data)) as Welcome | Frame;
    } catch {
      this.dispatchEvent(new CustomEvent("error", { detail: "bad frame" }));
      return;
    }
    this.receive(frame);
  }

  /**
   * Draw one message, as if a socket had brought it.
   *
   * A replay has no socket: its messages come out of a file at the times
   * they were recorded, and this is where a player hands each one over.
   * Lillecarl/pymux#610.
   */
  receive(frame: Welcome | Frame): void {
    if (frame.type === "welcome") {
      this.#writable = Boolean(frame.writable);
      this.#welcomed = true;
      this.#failures = 0;
      this.#adopt(this.#themeSheet, frame.css || "");
      this.#resize(frame.size);
      // A new stream has been told nothing yet.
      this.#toldSize = "";
      this.#tellTheSize();
      // Now, and not when the socket opened: this is the first moment a
      // listener can read `writable` and the size and be told the truth.
      this.dispatchEvent(new CustomEvent("connected"));
      return;
    }
    if (frame.type !== "frame") return;

    if (frame.whole) {
      // Every row the element holds was drawn under another size,
      // another reverse video or another palette, so none of it counts.
      //
      // **`palette` into its own sheet.** It is only the sixteen colours
      // and the two defaults, not the stylesheet the welcome sent. Both
      // were called `css` and went into one sheet, so the first frame
      // replaced every rule with the palette block: the screen lost its
      // background, its font, `white-space: pre`, the link rule and the
      // blink. A browser found it.
      if (frame.palette) this.#adopt(this.#paletteSheet, frame.palette);
      this.#resize(frame.size);
    }
    if (frame.styles) this.#learnStyles(frame.styles);
    for (const [number, runs] of Object.entries(frame.rows || {})) {
      this.#drawRow(Number(number), runs);
    }
    if (frame.cursor) this.#moveCursor(frame.cursor);
    for (const [number, png] of Object.entries(frame.images || {})) {
      this.#bitmaps.set(number, decoded(png));
    }
    if (frame.placements) this.#place(frame.placements);
  }

  #place(placements: Placement[]): void {
    const named = new Set(placements.map(([image]) => String(image)));
    for (const [number, bitmap] of this.#bitmaps) {
      if (named.has(number)) continue;
      this.#bitmaps.delete(number);
      void bitmap.then((held) => held?.close());
    }

    // Numbers only, so the rules cannot carry anything a program wrote.
    const rules: string[] = [];
    const canvases: HTMLCanvasElement[] = [];
    placements.forEach(([image, row, column, rows, columns, x, y, width, height], index) => {
      rules.push(
        `.image-${index} { top: calc(${row} * 1.2em); left: calc(${column} * 1ch); ` +
          `height: calc(${rows} * 1.2em); width: calc(${columns} * 1ch); }`,
      );
      const canvas = document.createElement("canvas");
      canvas.className = `image-${index}`;
      canvas.width = width;
      canvas.height = height;
      canvases.push(canvas);
      void this.#bitmaps.get(String(image))?.then((bitmap) => {
        if (bitmap) canvas.getContext("2d")?.drawImage(bitmap, x, y, width, height, 0, 0, width, height);
      });
    });
    this.#imageSheet.replaceSync(rules.join("\n"));
    this.#images.replaceChildren(...canvases);
  }

  /**
   * Adopt a stylesheet, widened so that the host sees the theme too.
   *
   * **The host needs the properties, not only the screen.** The server
   * writes its rules for `.pyte-screen`, so a custom property it defines
   * lives on the screen and inherits downward -- and the host, which is
   * above it, cannot see any of them. A page that gives the host padding
   * then shows its own background in that ring.
   *
   * So the rules are adopted twice, the second time with the screen's
   * selector widened to reach the host. A selector replacement and not a
   * hand-written copy: the values stay the server's, and `pyte.html`
   * remains the only place a colour is decided.
   */
  #adopt(sheet: CSSStyleSheet, css: string): void {
    sheet.replaceSync(css + "\n" + css.replaceAll(`.${SCREEN}`, ":host"));
  }

  #learnStyles(styles: Record<string, StyleEntry>): void {
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

  #resize(size: Size | undefined): void {
    if (!size) return;
    this.#screen.textContent = "";
    this.#rows = [];
    for (let index = 0; index < size.rows; index += 1) {
      const row = document.createElement("div");
      this.#rows.push(row);
      this.#screen.append(row);
    }
    // **The cursor lives in the screen, so emptying the screen takes it
    // away.** It is in there so that a page's padding moves the text and
    // the cursor together, and this is the price: every resize has to put
    // it back. A frame that resizes then carries a cursor position with
    // nothing to position. The images live there for the same reason.
    this.#screen.append(this.#cursor, this.#images);
  }

  #drawRow(number: number, runs: Run[]): void {
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
      let piece: HTMLAnchorElement | HTMLSpanElement;
      if (entry && entry.h) {
        const link = document.createElement("a");
        link.href = entry.h;
        link.rel = "noreferrer noopener";
        link.target = "_blank";
        piece = link;
      } else {
        piece = document.createElement("span");
      }
      // The rule this element built for the declarations, and the rules
      // the server's own stylesheet already holds.
      const names: string[] = [];
      if (entry && entry.s) names.push(`${STYLE_CLASS}${style}`);
      if (entry && entry.c) names.push(entry.c);
      if (names.length) piece.className = names.join(" ");
      piece.textContent = text;
      row.append(piece);
    }

    // A row with nothing in it still takes a line, the way a blank row
    // of a terminal does.
    if (!row.firstChild) row.append(document.createTextNode(" "));
  }

  #moveCursor(cursor: Cursor): void {
    const off = cursor.row < 0;
    this.#cursor.hidden = off;
    if (off) return;
    // The first rule is `:host`, which is where the two custom
    // properties the cursor is placed by are declared.
    const { style } = this.#ownSheet.cssRules[0] as CSSStyleRule;
    style.setProperty("--pymux-cursor-row", String(cursor.row));
    style.setProperty("--pymux-cursor-column", String(cursor.column));
  }

  // -- what a viewer types -------------------------------------------

  #onKeyDown(event: KeyboardEvent): void {
    const keys = keysFor(event);
    // `null` is "leave this keydown alone", which is what lets a plain
    // character reach `beforeinput` and a composition finish.
    if (keys === null) return;
    event.preventDefault();
    this.send({ type: INPUT, keys });
  }

  #onBeforeInput(event: InputEvent): void {
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

declare global {
  interface HTMLElementTagNameMap {
    "pymux-pane": PymuxPane;
  }
}

customElements.define("pymux-pane", PymuxPane);
