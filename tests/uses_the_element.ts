/**
 * Everything the element declares, used the way a consumer uses it.
 *
 * **This reads the declarations that ship.** The import below resolves to
 * `pymux/web/static/pymux-pane.d.ts`, which the compiler emits from the
 * TypeScript in `pymux/web/client/`. So it cannot drift from the code; what
 * it can be is unusable, and that is what this asks. A member that stops
 * being reachable fails a gate here rather than a consumer's build.
 *
 * It is not a test of behaviour and cannot be: `tsc` reads types and runs
 * nothing. What it says is that every member a consumer reaches for exists
 * with the shape the declarations promise. Lillecarl/pymux#461.
 */

import type {
  ClosedDetail,
  Cursor,
  Frame,
  InputMessage,
  PasteMessage,
  PymuxPane,
  Run,
  Size,
  StyleEntry,
  TextMessage,
  ViewerMessage,
  Welcome,
} from "../pymux/web/static/pymux-pane.js";

// The element, reached the way a page reaches it. The tag map is what
// makes this `PymuxPane` and not `HTMLElement`.
const element = document.querySelector("pymux-pane")!;

// A socket the page opened itself, so its cookie and Origin stay its own.
element.socket = new WebSocket("ws://127.0.0.1:8080/pane/%1001?t=x");

// Whether the server said this stream takes input. Read only.
const takesInput: boolean = element.writable;

// The three things a viewer may send.
const keys: InputMessage = { type: "input", keys: "C-c" };
const composed: TextMessage = { type: "text", text: "é" };
const pasted: PasteMessage = { type: "paste", text: "ls\n" };
const anyOfThem: ViewerMessage[] = [keys, composed, pasted];
for (const message of anyOfThem) element.send(message);

element.addEventListener("connected", (event) => {
  void event.type;
});
element.addEventListener("closed", (event) => {
  const why: ClosedDetail = event.detail;
  void why.code;
  void why.reason;
});
element.addEventListener("error", () => {});

element.close();

// The frames, as a relay would read them rather than as the element does.
declare const said: string;
const frame: Frame | Welcome = JSON.parse(said);

if (frame.type === "welcome") {
  const size: Size = frame.size;
  void size.columns;
  void size.rows;
  void frame.css;
  void frame.writable;
  void frame.revision;
} else {
  const cursor: Cursor = frame.cursor;
  void cursor.row;
  void cursor.column;

  const rows: Record<string, Run[]> = frame.rows;
  for (const runs of Object.values(rows)) {
    for (const [style, text] of runs) {
      void (style as number);
      void (text as string);
    }
  }

  const styles: Record<string, StyleEntry> | undefined = frame.styles;
  for (const entry of Object.values(styles ?? {})) {
    // Both are optional: a plain way of drawing has no declarations, and
    // a run with no hyperlink has no href.
    void entry.s;
    void entry.h;
  }

  void frame.whole;
  void frame.reverse;

  // The palette, which is not the stylesheet. A consumer that treated
  // them as one field lost every rule the welcome sent, so the two names
  // are load-bearing and this reaches for both.
  const palette: string | undefined = frame.palette;
  void palette;
}

void takesInput;
