/**
 * What a viewer's keydown sends to the program, and what it does not.
 *
 * **A module of its own so that a test can run it.** The element
 * subclasses `HTMLElement` and calls `customElements.define` when it is
 * imported, neither of which exists in node -- so a test of the element
 * needs a browser, and until this file there was nothing between "a
 * browser" and "nothing at all". Two faults reached a person that way.
 * This holds the part that is a table of cases, which is the part worth
 * running: `tests/keys.test.mjs` under `node --test`.
 * Lillecarl/pymux#468.
 *
 * Nothing here touches the DOM. `KeyPress` is structural, so a real
 * `KeyboardEvent` satisfies it and a test hands it an object literal.
 */

/** The parts of a keydown this reads. A `KeyboardEvent` is one. */
export interface KeyPress {
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  isComposing: boolean;
  keyCode: number;
}

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
export const NAMED_KEYS: Record<string, string | undefined> = {
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
export const TAKEN_BY_AN_IME = 229;

/**
 * Keys that are only a modifier, which type nothing on their own.
 *
 * **A keydown of one of these already reports itself held.** Pressing
 * Control gives `key` "Control" with `ctrlKey` true, so a reading of
 * "modified, and no text" sends `C-Control` to the program -- measured
 * by a person typing in a browser, and again by a headless one: Control
 * then Alt then Meta appended `C-ControlM-AltMeta`. Shift escaped it
 * only because the test below does not read `shiftKey`.
 *
 * A list and not `getModifierState(event.key)`: a lock key reports the
 * state it is about to leave, so that test answers wrongly for exactly
 * the keys that are hardest to notice.
 */
export const ONLY_A_MODIFIER = new Set([
  "Control",
  "Alt",
  "AltGraph",
  "Shift",
  "Meta",
  "CapsLock",
  "NumLock",
  "ScrollLock",
  "Fn",
  "FnLock",
  "Hyper",
  "Super",
  "Symbol",
  "SymbolLock",
]);

/**
 * The keys to send for one keydown, or `null` to send nothing.
 *
 * `null` means two different things and the caller treats them alike:
 * there is nothing to send, or the text of this key arrives through
 * `beforeinput` instead. In both cases the keydown is left alone, which
 * is what lets a composition and a plain character work.
 */
export function keysFor(event: KeyPress): string | null {
  // **Nothing while a composition runs.** A dead key and an IME both
  // deliver their keystrokes here as well, and the committed text
  // arrives separately; sending both would type it twice.
  if (event.isComposing || event.keyCode === TAKEN_BY_AN_IME) return null;

  // Holding a modifier is not typing, and the keydown that starts the
  // hold arrives with that modifier already set. So this comes before
  // anything reads one.
  if (ONLY_A_MODIFIER.has(event.key)) return null;

  const named = NAMED_KEYS[event.key];
  const modified = event.ctrlKey || event.altKey || event.metaKey;

  if (!named && !modified) return null; // `beforeinput` carries the text.

  const parts = [];
  if (event.ctrlKey) parts.push("C");
  if (event.altKey) parts.push("M");
  if (event.shiftKey && named) parts.push("S");
  parts.push(named || event.key);
  return parts.join("-");
}
