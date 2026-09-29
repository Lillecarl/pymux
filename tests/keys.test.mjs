/**
 * What a keydown sends, case by case.
 *
 * **The first test in this collection that runs the element's code.**
 * Everything else about `<pymux-pane>` is read as text or type checked,
 * and two faults reached a person that way -- the second was pressing
 * Control, which typed `C-Control` into the program.
 * Lillecarl/pymux#468.
 *
 * It reads the **compiled** `keys.js` out of `pymux/web/static/`, which
 * is what ships and what a browser runs. Plain javascript and not
 * TypeScript: `keysFor` is typed where it is written, node runs this
 * file as it stands, and a compiler in front of it would only add a step
 * between the test and the thing under test.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

import { keysFor } from "../pymux/web/static/keys.js";

/** A keydown, with everything a viewer did not do left off. */
function press(key, held = {}) {
  return {
    key,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    shiftKey: false,
    isComposing: false,
    keyCode: 0,
    ...held,
  };
}

test("a key that types a character is left to beforeinput", () => {
  // Not `null` because nothing happens: `beforeinput` is what carries
  // the text, and it is the only thing that sees an IME, a dead key or
  // a paste. A keydown that also sent this would type it twice.
  assert.equal(keysFor(press("a")), null);
  assert.equal(keysFor(press("é")), null);
  assert.equal(keysFor(press(" ")), null);
});

test("holding a modifier types nothing", () => {
  // The fault a person found: each of these arrives with its own
  // modifier already set, so a reading of "modified, and no text" built
  // `C-Control`, `M-Alt` and `Meta`.
  for (const held of [
    press("Control", { ctrlKey: true }),
    press("Alt", { altKey: true }),
    press("Meta", { metaKey: true }),
    press("Shift", { shiftKey: true }),
    press("AltGraph", { ctrlKey: true, altKey: true }),
    press("CapsLock"),
  ]) {
    assert.equal(keysFor(held), null, held.key);
  }
});

test("a modifier held with another key still sends", () => {
  // The other half of the guard above: the second keydown of Ctrl+C
  // carries `key` "c", so it is not a modifier press and must go.
  assert.equal(keysFor(press("c", { ctrlKey: true })), "C-c");
  assert.equal(keysFor(press("d", { ctrlKey: true })), "C-d");
  assert.equal(keysFor(press("Left", { altKey: true })), "M-Left");
  assert.equal(
    keysFor(press("Delete", { ctrlKey: true, altKey: true })),
    "C-M-DC",
  );
});

test("a key with no character of its own is named", () => {
  assert.equal(keysFor(press("Enter")), "Enter");
  assert.equal(keysFor(press("Backspace")), "BSpace");
  assert.equal(keysFor(press("ArrowUp")), "Up");
  assert.equal(keysFor(press("Insert")), "IC");
  assert.equal(keysFor(press("F5")), "F5");
});

test("Shift counts only for a named key", () => {
  // Shift plus a letter is already the capital letter, which
  // `beforeinput` carries. Shift plus Tab is a different key.
  assert.equal(keysFor(press("Tab", { shiftKey: true })), "S-Tab");
  assert.equal(keysFor(press("A", { shiftKey: true })), null);
});

test("a composition sends nothing, however it says so", () => {
  assert.equal(keysFor(press("a", { isComposing: true })), null);
  // Some browsers say it with `keyCode` instead of `isComposing`.
  assert.equal(keysFor(press("Process", { keyCode: 229 })), null);
  // Even a named key, because the composition owns the keyboard.
  assert.equal(keysFor(press("Enter", { isComposing: true })), null);
});
