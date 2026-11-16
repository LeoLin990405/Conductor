#!/usr/bin/env node
/* Headless check of the palette logic in palette.html (node, no packages).
 *
 *   node docs/design/terminal-native/web/check_palette.js
 *
 * Runs the page's own <script> against a tiny DOM stub, then drives the
 * matcher (D1) and argument completion (D2) through the same input path the
 * keyboard uses. Exit status 0 when every expectation holds. */
"use strict";
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "palette.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length !== 1) throw new Error("expected one inline script, got " + scripts.length);

/* ---- a DOM just large enough for the page script ---- */
class El {
  constructor(tag, id) {
    this.tagName = (tag || "div").toUpperCase(); this.id = id || ""; this.children = [];
    this.attrs = {}; this.listeners = {}; this.hidden = false; this.value = "";
    this._text = ""; this.innerHTML = ""; this.parentNode = null;
    this.classList = { add() {}, remove() {} };
    this.selectionStart = 0; this.selectionEnd = 0;
  }
  set textContent(v) { this._text = String(v); if (v === "") this.children = []; }
  get textContent() { return this._text; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatch(t, ev) { (this.listeners[t] || []).forEach(f => f.call(this, ev)); }
  focus() { doc.activeElement = this; }
  closest(sel) {
    for (let n = this; n; n = n.parentNode) if (sel === "li[data-i]" && n.tagName === "LI" && n.hasAttribute("data-i")) return n;
    return null;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  scrollIntoView() {}
}
const byId = {};
const doc = {
  documentElement: new El("html"), body: new El("body"), activeElement: null, listeners: {},
  getElementById(id) { return byId[id] || (byId[id] = new El("div", id)); },
  createElement(t) { return new El(t); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  contains() { return true; },
};
doc.documentElement.setAttribute("data-theme", "dark");
byId["palette-list"] = new El("ul", "palette-list");
byId["palette-input"] = new El("input", "palette-input");
byId["palette"] = new El("div", "palette");
global.document = doc;
global.window = { matchMedia: () => ({ matches: false }), scrollBy() {} };
global.location = { search: "", href: "" };
global.URLSearchParams = URLSearchParams;
new Function("window", "document", "location", scripts[0])(global.window, doc, global.location);
const P = global.window.__palette;
const input = byId["palette-input"], list = byId["palette-list"], status = byId["palette-status"];

let fails = 0, passes = 0;
function ok(cond, label) { if (cond) passes++; else { fails++; console.log("FAIL", label); } }
function type(v) { input.value = v; input.dispatch("input", {}); }
function key(k, extra) {
  const ev = Object.assign({ key: k, code: "", shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, extra || {});
  input.dispatch("keydown", ev); return ev;
}
const names = () => P.rows().map(r => r.name);

/* ---- D1 matcher ---- */
const expect = { dsp: "dispatch", DSP: "dispatch", Disp: "dispatch", jobs: "tickets", tkt: "tickets", apv: "approve" };
for (const [q, top] of Object.entries(expect)) {
  type(q);
  ok(names()[0] === top, `${q} -> ${top} first (got ${names().join(",")})`);
  console.log(`  ${q.padEnd(5)} -> ${names().slice(0, 4).join(", ")}`);
}
type("dsp"); ok(names().length === 1, "dsp has exactly one row");
const d = P.fz("dsp", "dispatch"); ok(d.gap === 1 && d.idx.join() === "0,2,3", "dsp gap cost 1 at 0,2,3");
ok(P.fz("DSP", "dispatch").gap === 1, "DSP folds to dsp");
ok(P.fz("tkt", "tickets").gap === 3, "tkt gap 3");
ok(P.fz("apv", "approve").gap === 3, "apv gap 3");
type("jobs"); ok(P.rows()[0].meta === "jobs" && !P.rows()[0].onName, "jobs ranks tickets via the alias haystack");
type("/jobs"); ok(names()[0] === "tickets", "/jobs strips one leading slash");
type("zzz"); ok(P.rows().length === 0 && /no command matches/.test(P.view().note), "zzz -> empty state 'no command matches'");
ok(list.children.length === 1 && /no command matches/.test(list.children[0].textContent), "zzz paints the empty row");
type("me"); ok(names().slice(0, 4).join() === "memory,merge,resume,model", "me -> memory, merge, resume, model (got " + names().slice(0, 4).join() + ")");
type(""); ok(names().length === 28 && names()[0] === "workers" && names()[2] === "approve", "empty needle -> idleOrder, 28 rows");

/* ---- D2 argument completion ---- */
type("dispatch "); ok(names().length === 4 && names()[0] === "7f3a91c2", "dispatch + space lists 4 dispatch ids");
key("Tab"); ok(input.value === "dispatch 7f3a91c2", "Tab inserts the highlighted dispatch id (got '" + input.value + "')");
type("dispatch 7"); ok(names().length === 2, "ambiguous prefix lists both matches, inserts nothing");
type("dispatch 9"); ok(names().join() === "91cc6a40", "unique prefix narrows to 91cc6a40");
type("dispatch DSP-9"); ok(names().length === 0, "hex id prefix is case-sensitive");
type("dispatch "); key("Enter"); ok(/^ran: dispatch$/.test(status.textContent), "Enter on 'dispatch ' runs the roster (got '" + status.textContent + "')");

type("approve "); ok(names().join() === "7f3a91c2", "approve offers only awaiting_human ids");
type("reject "); ok(names().join() === "7f3a91c2", "reject offers only awaiting_human ids");
type("approve "); key("Enter");
ok(input.value === "approve 7f3a91c2" && !/^ran:/.test(status.textContent), "Enter on bare 'approve ' inserts the id, does not run");
key("Enter"); ok(status.textContent === "ran: approve 7f3a91c2", "second Enter runs approve <id>");
type("approve 4"); key("Enter");
ok(!/^ran:/.test(status.textContent) && /approve needs a dispatch id/.test(status.textContent) && /approve 需要一个 dispatch id/.test(status.textContent),
   "approve with no offered id shows the bilingual hint (got '" + status.textContent + "')");
ok(list.children.length === 1 && /approve 需要一个 dispatch id/.test(list.children[0].textContent), "the row shows the hint");
type("approve"); key("Enter"); ok(input.value === "approve " && !/^ran:/.test(status.textContent), "Enter on the approve command row inserts 'approve ' and never runs");
type("appr"); key("Tab"); ok(input.value === "approve ", "Tab on approve adds a trailing space (it needs an argument)");
type("disp"); key("Tab"); ok(input.value === "dispatch", "Tab on dispatch adds no space (argument optional)");
type("reply "); key("Enter"); ok(/reply needs text/.test(status.textContent), "bare reply does not run");
type("reply 好的"); key("Enter"); ok(status.textContent === "ran: reply 好的", "reply with text runs");
type("status"); key("Enter"); ok(/^ran: status/.test(status.textContent), "no-argument command runs on Enter");
type("tickets 9f2c1ab"); ok(names().length === 2, "9f2c1ab lists two known ids");
type("tickets 9f2c1ab7"); ok(names().length === 1, "9f2c1ab7 narrows to one");

/* ---- keys ---- */
type("dsp"); let ev = key("Tab", { shiftKey: true }); ok(ev.defaultPrevented && input.value === "dsp", "Shift+Tab is inert inside the palette (does not accept)");
type(""); key("j"); ok(true, "j in the query is not intercepted");
ev = key("j"); ok(!ev.defaultPrevented, "j is typed in an empty query (navigation mode off)");
ev = key("n", { altKey: true, code: "KeyN" }); ok(ev.defaultPrevented && !byId["palette-nav"].hidden, "Alt+N turns navigation mode on; chrome shows nav");
ev = key("j"); ok(ev.defaultPrevented, "j moves the highlight in navigation mode");
key("n", { altKey: true, code: "KeyN" });

/* ---- mouse: every row activates, by its own index ---- */
let clickedAll = true;
for (let i = 0; i < 28; i++) {
  type("");
  const li = list.children[i];
  const want = P.rows()[i].name;
  list.dispatch("click", { target: li });
  const got = status.textContent + " | " + input.value;
  if (!(got.indexOf(want) > -1)) { clickedAll = false; console.log("  click row", i, want, "->", got); }
}
ok(clickedAll, "clicking each of the 28 idle rows acts on that row");
type("dispatch "); list.dispatch("click", { target: list.children[2] });
ok(input.value === "dispatch 91cc6a40", "clicking the third argument row inserts that id (got '" + input.value + "')");
type("approve"); list.dispatch("click", { target: list.children[0] });
ok(input.value === "approve ", "clicking the approve command row does what Enter does");

console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
