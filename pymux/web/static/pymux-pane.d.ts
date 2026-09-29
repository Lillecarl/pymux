/**
 * Types for `<pymux-pane>`.
 *
 * **Hand-written, beside the hand-written `.js`.** Nothing compiles this
 * package: there is no node in any build of pymux, which is the point.
 * So these can drift from the code, and `tests/type_check_the_element.ts`
 * is what stops that -- it is a usage sample that `tsc` reads against
 * this file, so a signature that no longer matches fails a check rather
 * than a consumer's build. Lillecarl/pymux#461.
 */

/** One run of a row: the number of a way of drawing, and its characters. */
export type Run = [style: number, text: string];

/** What a style number means, sent once and referenced by number after. */
export interface StyleEntry {
  /** The CSS declarations for a cell drawn this way. */
  s?: string;
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
}

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

export type ViewerMessage = InputMessage | TextMessage | PasteMessage;

export interface ClosedDetail {
  code: number;
  reason: string;
}

export declare class PymuxPane extends HTMLElement {
  /**
   * The socket to read frames from.
   *
   * Assigning one hands the element a socket the page opened itself, so
   * a cookie and an `Origin` stay the page's own. The element does not
   * close a socket it was given.
   */
  socket: WebSocket | null;

  /** Whether the server said this stream takes input. */
  readonly writable: boolean;

  /** Send one message as it stands. */
  send(message: ViewerMessage): void;

  /** Close a socket this element opened. */
  close(): void;

  addEventListener(
    type: "connected" | "error",
    listener: (event: CustomEvent) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
  addEventListener(
    type: "closed",
    listener: (event: CustomEvent<ClosedDetail>) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: boolean | AddEventListenerOptions,
  ): void;
}

declare global {
  interface HTMLElementTagNameMap {
    "pymux-pane": PymuxPane;
  }
}
