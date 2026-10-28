// A stand-in browser for kosher_calm.js, enough to drive it from node:
//   node calm_harness.js <kosher_calm.js> <site> <mode> <items> <path> <budget>
//
// `budget` is the remaining seconds on the tag (-1 = no daily budget). It
// prints one JSON line: which feed items were hidden, whether the end card
// was added, which sidebar/rail elements were hidden, and the heartbeat
// bodies the proxy was sent.
//
// The DOM here is small on purpose: the elements the script knows by name,
// and a selector matcher for the subset it uses (`tag`, `#id`,
// `[attr="v"]`, `[attr^="v"]`, joined by commas).
"use strict";
const fs = require("fs");
const [, , scriptPath, site, mode, itemsArg, pathArg, budgetArg] = process.argv;
const items = parseInt(itemsArg, 10);
const budget = parseInt(budgetArg, 10);
const TOKEN = "nonce.signature-for-this-page";

function parseSimple(part) {
  const out = { tag: null, id: null, attrs: [] };
  const rest = part.trim();
  const tag = rest.match(/^[A-Za-z][\w-]*/);
  let i = 0;
  if (tag) { out.tag = tag[0].toLowerCase(); i = tag[0].length; }
  while (i < rest.length) {
    if (rest[i] === "#") {
      const m = rest.slice(i + 1).match(/^[\w-]+/);
      if (!m) return null;
      out.id = m[0]; i += 1 + m[0].length;
    } else if (rest[i] === "[") {
      const m = rest.slice(i).match(/^\[([\w-]+)(?:(\^?=)"([^"]*)")?\]/);
      if (!m) return null;
      out.attrs.push([m[1], m[2] || null, m[3] === undefined ? null : m[3]]);
      i += m[0].length;
    } else return null;
  }
  return out;
}

function parseSelector(selector) {
  return String(selector).split(",").map(parseSimple).filter(Boolean);
}

function matches(el, simple) {
  if (simple.tag && el.tag !== simple.tag) return false;
  if (simple.id && el.id !== simple.id) return false;
  for (const [name, op, value] of simple.attrs) {
    const have = el.attrs[name];
    if (have === undefined) return false;
    if (op === "=" && have !== value) return false;
    if (op === "^=" && !String(have).startsWith(value)) return false;
  }
  return true;
}

class El {
  constructor(tag, attrs) {
    this.nodeType = 1;
    this.tag = String(tag).toLowerCase();
    this.nodeName = this.tag.toUpperCase();
    this.attrs = Object.assign({}, attrs || {});
    this.id = this.attrs.id || "";
    this.children = [];
    this.parentNode = null;
    this.textContent = "";
    this.hiddenByStyle = null;
    const self = this;
    this.style = {
      cssText: "",
      setProperty(name, value) { if (name === "display" && value === "none") self.hiddenByStyle = true; },
    };
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const list = this.parentNode.children;
    const at = list.indexOf(this);
    return at >= 0 && at + 1 < list.length ? list[at + 1] : null;
  }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, before) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const at = before ? this.children.indexOf(before) : -1;
    if (at < 0) this.children.push(child); else this.children.splice(at, 0, child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    const at = this.children.indexOf(child);
    if (at >= 0) this.children.splice(at, 1);
    child.parentNode = null;
    return child;
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === "id") this.id = String(value);
  }
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; }
  descendants() {
    const out = [];
    for (const child of this.children) { out.push(child); out.push(...child.descendants()); }
    return out;
  }
  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    return this.descendants().filter((el) => groups.some((s) => matches(el, s)));
  }
  closest(selector) {
    const groups = parseSelector(selector);
    for (let el = this; el; el = el.parentNode) {
      if (groups.some((s) => matches(el, s))) return el;
    }
    return null;
  }
}

function el(tag, attrs, children) {
  const node = new El(tag, attrs);
  (children || []).forEach((child) => node.appendChild(child));
  return node;
}

// A page shaped like the sites': a feed of posts, a sidebar, and (for the
// single pages) a comment tree and a related rail.
const page = el("html", {}, []);
const body = el("body", {}, []);
page.appendChild(body);
const main = el("main", {}, []);
body.appendChild(main);

const feedItems = [];
const CELL = {
  x: () => el("div", { "data-testid": "cellInnerDiv" }, [el("p", {}, [])]),
  reddit: () => el("shreddit-post", {}, [el("p", {}, [])]),
  youtube: () => el("ytd-rich-item-renderer", {}, [el("p", {}, [])]),
  facebook: () => el("div", { role: "article" }, [el("p", {}, [])]),
}[site] || (() => el("div", { "data-testid": "cellInnerDiv" }, [el("p", {}, [])]));
for (let n = 0; n < 6; n++) {
  const cell = CELL();
  cell.textContent = "post " + n;
  feedItems.push(cell);
  main.appendChild(cell);
}
const sidebar = el("div", { id: "secondary", "data-testid": "sidebarColumn" }, []);
const related = el("div", { id: "related" }, []);
const comments = el("ytd-comments", {}, []);
const roleAside = el("div", { role: "complementary" }, []);
body.appendChild(sidebar);
body.appendChild(related);
body.appendChild(comments);
body.appendChild(roleAside);

let observerCallback = null;
const timers = [];
const intervals = [];
let beats = [];

class FakeXHR {
  open(method, url) { this.method = method; this.url = url; this.status = 0; this.headers = {}; }
  setRequestHeader(name, value) { this.headers[name.toLowerCase()] = value; }
  send(payload) {
    if (this.headers["x-kosheros-page"] !== TOKEN) { this.status = 403; this.responseText = "{}"; }
    else if (this.url.endsWith("calm-beat")) {
      beats.push(JSON.parse(payload));
      this.status = 200;
      this.responseText = JSON.stringify({ remaining: 60, over: false });
    } else { this.status = 404; this.responseText = "{}"; }
    if (this.onload) this.onload();
  }
}

global.window = { addEventListener() {} };
global.location = { href: "https://x.com" + pathArg, pathname: pathArg };
global.history = { pushState() {}, replaceState() {} };
global.XMLHttpRequest = FakeXHR;
global.MutationObserver = class { constructor(cb) { observerCallback = cb; } observe() {} disconnect() {} };
global.setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
global.clearInterval = () => {};
const TAG = {
  "data-kosheros-calm-site": site,
  "data-kosheros-calm-mode": mode,
  "data-kosheros-calm-items": String(items),
  "data-kosheros-calm-remaining": String(budget),
  "data-kosheros-calm-rules": JSON.stringify({
    x: { login: ["/i/flow/"], single: ["^/[^/]+/status/\\d+"] },
    reddit: { login: ["/login"], single: ["^/r/[^/]+/comments/"] },
    youtube: { login: [], single: ["^/watch", "^/shorts/", "^/embed/"] },
    facebook: { login: ["/login"], single: ["^/[^/]+/posts/"] },
  }[site] || {}),
  "data-kosheros-token": TOKEN,
};
global.document = {
  currentScript: {
    getAttribute: (k) => (TAG[k] === undefined ? null : TAG[k]),
    removeAttribute() {},
  },
  readyState: "complete",
  documentElement: page,
  body: body,
  visibilityState: "visible",
  createElement: (tag) => new El(tag, {}),
  getElementById: (id) => page.descendants().find((x) => x.id === id) || null,
  querySelectorAll: (sel) => page.querySelectorAll(sel),
  addEventListener() {},
  open() {}, write() {}, close() {},
};

eval(fs.readFileSync(scriptPath, "utf8"));
// What the app draws next: the observer fires and the debounce runs.
if (observerCallback) observerCallback([{ type: "childList", addedNodes: [] }]);
for (const fn of timers) fn();
// One heartbeat, if the account has a budget: fifteen seconds have passed
// since the script started watching.
const realNow = Date.now;
Date.now = () => realNow() + 15000;
for (const fn of intervals) fn();
Date.now = realNow;

process.stdout.write(JSON.stringify({
  hidden: feedItems.map((cell) => !!cell.hiddenByStyle),
  endCard: !!document.getElementById("__kosheros_calm_end"),
  sidebarHidden: !!sidebar.hiddenByStyle,
  relatedHidden: !!related.hiddenByStyle,
  commentsHidden: !!comments.hiddenByStyle,
  asideHidden: !!roleAside.hiddenByStyle,
  beats: beats,
}) + "\n");
