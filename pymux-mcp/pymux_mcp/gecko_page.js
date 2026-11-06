// What pymux-mcp does inside a tab of Firefox or Glide. `gecko.py` loads
// this once into a system-principal sandbox of a content process, and
// keeps it on that process's shared system global, so a ref survives the
// connection that made it. Each call names a tab by its browsing
// context's id, and answers JSON.
//
// The DevTools console of a content process evaluates in a sandbox that
// lacks `BrowsingContext`; the system global that holds `Services` has it.
// Measured on Gecko 159.

"use strict";

const acc = Cc["@mozilla.org/accessibilityService;1"].getService(Ci.nsIAccessibilityService);

// Gecko's role names, as the ARIA names Chromium's tree uses, so a
// snapshot reads the same whichever browser drew it.
const ROLES = {
  pushbutton: "button",
  "toggle button": "button",
  buttondropdown: "button",
  buttonmenu: "button",
  checkbutton: "checkbox",
  radiobutton: "radio",
  entry: "textbox",
  "password text": "textbox",
  editcombobox: "combobox",
  "combobox list": "listbox",
  "combobox option": "option",
  "listbox option": "option",
  "listbox rich option": "option",
  outline: "tree",
  outlineitem: "treeitem",
  pagetab: "tab",
  pagetablist: "tablist",
  graphic: "img",
  "text leaf": "StaticText",
  statictext: "StaticText",
  document: "RootWebArea",
  section: "generic",
  "text container": "generic",
  text: "generic",
  grouping: "group",
  "check menu item": "menuitemcheckbox",
  "radio menu item": "menuitemradio",
  "parent menuitem": "menuitem",
  whitespace: "none",
  "list item marker": "ListMarker",
  "internal frame": "Iframe",
  label: "LabelText",
};
const SKIPPED = new Set(["none", "generic"]);
const NO_REF = new Set(["RootWebArea", "StaticText"]);
const CLIMBED = new Set([...NO_REF, "none"]);

// Each window's refs, by its inner window id: a number for each node, and
// the node for each number. A new document starts again at 1, so an old
// ref says it is gone rather than naming something else.
const windows = new Map();

function refsOf(win) {
  const id = win.windowGlobalChild.innerWindowId;
  for (const [known] of windows) {
    if (!WindowGlobalChild.getByInnerWindowId(known)) windows.delete(known);
  }
  let refs = windows.get(id);
  if (!refs) {
    refs = { next: 1, byId: new Map(), byNode: new WeakMap() };
    windows.set(id, refs);
  }
  return refs;
}

function refFor(win, node) {
  const refs = refsOf(win);
  let id = refs.byNode.get(node);
  if (id === undefined) {
    id = refs.next++;
    refs.byNode.set(node, id);
    refs.byId.set(id, new WeakRef(node));
  }
  return "e" + id;
}

function nodeFor(win, ref) {
  const match = /^e(\d+)$/.exec(ref);
  if (!match) throw new Error(`${JSON.stringify(ref)} is not a ref; browser.snapshot answers them, as e12`);
  const node = refsOf(win).byId.get(Number(match[1]))?.deref();
  if (!node || !node.isConnected) throw new Error(`the ref ${ref} is gone; take a new browser.snapshot`);
  return node;
}

function windowOf(context) {
  const win = BrowsingContext.get(context)?.window;
  if (!win) throw new Error(`no tab ${context} in this process`);
  return win;
}

function role(accessible) {
  const name = acc.getStringRole(accessible.role);
  return ROLES[name] ?? name;
}

function element(accessible) {
  const node = accessible.DOMNode;
  return node && node.nodeType === 1 ? node : null;
}

function children(accessible) {
  const found = [];
  for (let child = accessible.firstChild; child; child = child.nextSibling) found.push(child);
  return found;
}

// The service builds a document's tree on a later tick: an empty document
// node right after the service starts in a process. Measured on Gecko 159.
async function documentOf(win) {
  for (let tries = 0; tries < 40; tries++) {
    const found = acc.getAccessibleFor(win.document);
    if (found && (found.childCount > 0 || !win.document.body?.hasChildNodes())) return found;
    await new Promise(resolve => win.setTimeout(resolve, 50));
  }
  throw new Error("the page has no accessibility tree after 2s");
}

function line(win, accessible, depth) {
  const shown = role(accessible);
  let text = `${"  ".repeat(depth)}- ${shown}`;
  if (accessible.name) text += ` ${JSON.stringify(accessible.name)}`;
  // A link's value is its address, which Chromium's tree leaves out.
  if (accessible.value && shown !== "link") text += ` value=${JSON.stringify(accessible.value)}`;
  const node = element(accessible);
  if (node && !NO_REF.has(shown)) text += ` [ref=${refFor(win, node)}]`;
  return text;
}

function accessibleOf(win, ref) {
  const found = acc.getAccessibleFor(nodeFor(win, ref));
  if (!found) throw new Error(`the ref ${ref} has no node in the accessibility tree`);
  return found;
}

const calls = {
  async snapshot(win, { limit, within }) {
    const lines = [`# ${win.document.title}`, `# ${win.location.href}`];
    const walk = (accessible, depth) => {
      if (lines.length >= limit) return;
      const shown = !SKIPPED.has(role(accessible));
      if (shown) lines.push(line(win, accessible, depth));
      for (const child of children(accessible)) walk(child, shown ? depth + 1 : depth);
    };
    walk(within ? accessibleOf(win, within) : await documentOf(win), 0);
    if (lines.length >= limit) lines.push(`... cut at ${limit} lines`);
    return lines.join("\n");
  },

  async find(win, { text, limit }) {
    const wanted = text.toLowerCase();
    const lines = [];
    const seen = new Set();
    const walk = accessible => {
      if (lines.length >= limit) return;
      const said = accessible.name || accessible.value || "";
      if (said.toLowerCase().includes(wanted)) {
        let holder = accessible;
        while (holder && (CLIMBED.has(role(holder)) || !element(holder))) holder = holder.parent;
        if (holder && !seen.has(holder)) {
          seen.add(holder);
          lines.push(`- ${role(holder)} ${JSON.stringify(said)} [ref=${refFor(win, element(holder))}]`);
        }
      }
      for (const child of children(accessible)) walk(child);
    };
    walk(await documentOf(win));
    return lines;
  },

  at(win, { x, y, actionable }) {
    const node = win.document.elementFromPoint(x, y);
    if (!node) throw new Error(`nothing at ${x}, ${y}: it is outside the page`);
    return holding(win, node, actionable) ?? null;
  },

  // Scrolled to the middle of the view first, as a person would see it.
  centre(win, { ref, selector }) {
    const node = target(win, ref, selector);
    node.scrollIntoView({ block: "center", inline: "center" });
    const box = node.getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  },

  click(win, { x, y }) {
    eventUtils(win).synthesizeMouseAtPoint(x, y, {}, win);
    return null;
  },

  type(win, { text, ref, selector, clear }) {
    if (ref || selector) {
      const node = target(win, ref, selector);
      node.focus();
      if (clear) node.value = "";
    }
    eventUtils(win).sendString(text, win);
    return null;
  },

  press(win, { key }) {
    eventUtils(win).synthesizeKey(key.length === 1 ? key : `KEY_${key}`, {}, win);
    return null;
  },

  point(win, { ref, selector, label, seconds, colour }) {
    const node = target(win, ref, selector);
    node.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const box = node.getBoundingClientRect();
    const doc = win.document;
    const mark = doc.createElement("div");
    mark.setAttribute("data-pymux-point", "");
    Object.assign(mark.style, {
      position: "fixed",
      left: `${box.left - 4}px`,
      top: `${box.top - 4}px`,
      width: `${box.width + 8}px`,
      height: `${box.height + 8}px`,
      outline: `3px solid ${colour}`,
      borderRadius: "4px",
      boxShadow: "0 0 0 100vmax rgba(0, 0, 0, 0.25)",
      pointerEvents: "none",
      zIndex: "2147483647",
    });
    if (label) {
      const tag = doc.createElement("div");
      tag.textContent = label;
      Object.assign(tag.style, {
        position: "absolute",
        left: "0",
        background: colour,
        color: "white",
        font: "bold 13px sans-serif",
        padding: "2px 6px",
        borderRadius: "3px",
        whiteSpace: "nowrap",
      });
      tag.style[box.top < 28 ? "top" : "bottom"] = "calc(100% + 4px)";
      mark.append(tag);
    }
    doc.documentElement.append(mark);
    win.setTimeout(() => mark.remove(), seconds * 1000);
    return null;
  },

  selection(win) {
    const field = win.document.activeElement;
    let text, around;
    if (field && typeof field.selectionStart === "number" && field.selectionEnd > field.selectionStart) {
      text = field.value.slice(field.selectionStart, field.selectionEnd);
      around = field;
    } else {
      const selected = win.getSelection();
      if (!selected || selected.isCollapsed) return null;
      text = selected.toString();
      around = selected.getRangeAt(0).commonAncestorContainer;
      if (around.nodeType !== 1) around = around.parentElement;
    }
    return { text, ...(holding(win, around, false) ?? {}) };
  },

  metrics(win) {
    const doc = win.document.documentElement;
    return {
      x: win.scrollX,
      y: win.scrollY,
      width: doc.clientWidth || win.innerWidth,
      height: doc.clientHeight || win.innerHeight,
      pageWidth: Math.max(doc.scrollWidth, doc.clientWidth),
      pageHeight: Math.max(doc.scrollHeight, doc.clientHeight),
      density: win.devicePixelRatio,
    };
  },

  // Each element a person acts on with a box inside `clip`, in page
  // pixels, that a click at its centre reaches: what is topmost there is
  // the element or inside it, so a dialog over the page hides the links
  // under it.
  async marks(win, { clip, actionable }) {
    const wanted = new Set(actionable);
    const found = [];
    const seen = new Set();
    const walk = accessible => {
      const shown = role(accessible);
      const node = element(accessible);
      if (wanted.has(shown) && node && !seen.has(node)) {
        seen.add(node);
        const box = node.getBoundingClientRect();
        const x = box.x + win.scrollX;
        const y = box.y + win.scrollY;
        const inside =
          box.width > 0 &&
          box.height > 0 &&
          x < clip.x + clip.width &&
          x + box.width > clip.x &&
          y < clip.y + clip.height &&
          y + box.height > clip.y;
        const hit = inside && win.document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        if (hit && (hit === node || node.contains(hit))) {
          found.push({
            ref: refFor(win, node),
            role: shown,
            name: accessible.name || "",
            x,
            y,
            width: box.width,
            height: box.height,
          });
        }
      }
      for (const child of children(accessible)) walk(child);
    };
    walk(await documentOf(win));
    return found;
  },

  // What the page logged and threw, oldest first: the console API's
  // stored calls and the console service's errors for this document.
  console(win, { limit }) {
    const id = win.windowGlobalChild.innerWindowId;
    const said = [];
    const storage = Cc["@mozilla.org/consoleAPI-storage;1"].getService(Ci.nsIConsoleAPIStorage);
    for (const event of storage.getEvents(String(id))) {
      const words = Array.from(event.arguments ?? [], one => {
        try {
          return typeof one === "object" && one !== null ? JSON.stringify(one) : String(one);
        } catch {
          return String(one);
        }
      });
      said.push({ type: event.level, text: words.join(" "), at: event.timeStamp });
    }
    // Errors only: Gecko reports a warning, such as quirks mode, as a script error too.
    const quiet = Ci.nsIScriptError.warningFlag | Ci.nsIScriptError.infoFlag;
    for (const message of Services.console.getMessageArray() ?? []) {
      if (message instanceof Ci.nsIScriptError && message.innerWindowID === id && !(message.flags & quiet)) {
        said.push({ type: "exception", text: message.errorMessage, at: message.microSecondTimeStamp / 1000 });
      }
    }
    said.sort((a, b) => a.at - b.at);
    return said.slice(-limit).map(({ type, text }) => ({ type, text }));
  },

  // With the page's principal and its window as the global, as a page
  // script runs; not `win.eval`, which a page's CSP may forbid.
  async evaluate(win, { expression }) {
    const sandbox = Cu.Sandbox(win, { sandboxPrototype: win, wantXrays: false, sandboxName: "pymux-mcp evaluate" });
    const run = `(async () => {
      const value = await eval(${JSON.stringify(expression)});
      return value === undefined ? "null" : (JSON.stringify(value) ?? "null");
    })()`;
    return JSON.parse(await Cu.evalInSandbox(run, sandbox, "latest", "resource://pymux-mcp/evaluate.js", 1));
  },
};

function target(win, ref, selector) {
  if (!ref === !selector) throw new Error("name the element by `ref` or by `selector`, one of them");
  if (ref) return nodeFor(win, ref);
  const found = win.document.querySelector(selector);
  if (!found) throw new Error(`nothing matches ${JSON.stringify(selector)}`);
  return found;
}

// The nearest element the accessibility tree names that holds `node`:
// one a person acts on first, when `actionable` lists those roles.
function holding(win, node, actionable) {
  let accessible = null;
  for (let at = node; at && !accessible; at = at.parentNode) accessible = acc.getAccessibleFor(at);
  const chain = [];
  for (; accessible; accessible = accessible.parent) {
    const shown = role(accessible);
    if (element(accessible) && !SKIPPED.has(shown) && !NO_REF.has(shown)) chain.push(accessible);
  }
  const wanted = actionable ? new Set(actionable) : null;
  const found = (wanted && chain.find(one => wanted.has(role(one)))) || chain[0];
  if (!found) return null;
  return { ref: refFor(win, element(found)), role: role(found), name: found.name || "" };
}

// The EventUtils Marionette synthesizes input with: events the page sees
// as trusted, as a person's are. One copy a window.
const eventUtilsOf = new WeakMap();

function eventUtils(win) {
  let utils = eventUtilsOf.get(win);
  if (!utils) {
    utils = { window: win, parent: win, _EU_Ci: Ci, _EU_Cc: Cc };
    Services.scriptloader.loadSubScript("chrome://remote/content/external/EventUtils.js", utils);
    eventUtilsOf.set(win, utils);
  }
  return utils;
}

// eslint-disable-next-line no-unused-vars
async function call(name, context, args) {
  const found = calls[name];
  if (!found) throw new Error(`no call ${name}`);
  return JSON.stringify(await found(windowOf(context), args ?? {}));
}
