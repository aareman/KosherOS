// A stand-in browser for kosher_page.js, enough to drive it from node:
//   node page_filter_harness.js <kosher_page.js> <words.json> <mode> <text>...
// Prints one line per text: the text after the page filter has seen it,
// then a line "judged: <text sent to the proxy>" (or "judged: -").
"use strict";
const fs = require("fs");
const [, , scriptPath, wordsPath, mode, ...texts] = process.argv;
const words = fs.readFileSync(wordsPath, "utf8");
let observerCallback = null;
let judged = [];
let timers = [];

class FakeXHR {
  open(method, url) { this.method = method; this.url = url; this.status = 0; }
  setRequestHeader() {}
  send(body) {
    if (this.url.endsWith("words.json")) { this.status = 200; this.responseText = words; }
    else if (this.url.endsWith("judge")) { judged.push(JSON.parse(body)); this.status = 200; this.responseText = JSON.stringify({ blocked: false }); }
    if (this.onload) this.onload();
  }
}

const nodes = texts.map((t) => ({ nodeType: 3, data: t, parentNode: { nodeType: 1, nodeName: "P", isContentEditable: false, parentNode: null } }));

global.window = {};
global.location = { href: "https://example.test/page" };
global.XMLHttpRequest = FakeXHR;
global.NodeFilter = { SHOW_TEXT: 4 };
global.MutationObserver = class { constructor(cb) { observerCallback = cb; } observe() {} };
global.setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
global.document = {
  currentScript: { getAttribute: (k) => (k === "data-kosheros-language" ? mode : k === "data-kosheros-judge" ? "1" : null) },
  body: { nodeType: 1, nodeName: "BODY", isContentEditable: false },
  documentElement: {},
  createTreeWalker: () => { let i = -1; return { nextNode: () => (++i < 0 ? null : null) }; },
  open() {}, write() {}, close() {},
};

eval(fs.readFileSync(scriptPath, "utf8"));
// What the page "adds" after load: one record with every text node.
observerCallback([{ type: "childList", addedNodes: nodes }]);
for (const fn of timers) fn();   // flush the judge batch
for (const n of nodes) process.stdout.write(n.data + "\n");
process.stdout.write("judged: " + (judged.length ? judged.map((j) => j.text || ("language=" + j.language)).join(" | ") : "-") + "\n");
