/* KosherOS page filter: what the proxy cannot see, the page shows it.
 *
 * The proxy cleans and judges a page's HTML on the way in. What a page
 * draws afterwards — a chat message, a feed item, a search result
 * fetched as JSON and rendered by script — never passes through it as
 * text. This script is served by the proxy on a reserved path, injected
 * into every page it filters, and does two things in the page:
 *
 *   1. Language: the same word list the proxy uses, as the same patterns,
 *      run over every text node the page adds or changes. In substitute
 *      mode the word is replaced in place; in block mode the proxy is
 *      asked for its block page.
 *   2. Content: the words the page adds are gathered and sent to the
 *      proxy to be judged by the same scorer, with the same tolerance for
 *      this account, that judged the page itself. A page that renders
 *      what the filter would have blocked is blocked a moment later.
 *
 * Nothing leaves the machine: the reserved path is answered by the proxy
 * on this computer and never forwarded. The script keeps no state and
 * reads nothing but the page. The tag it came on carries a token for
 * this page; the proxy answers the word list and the judge only to a
 * call that sends it back.
 */
(function () {
  "use strict";
  var ROOT = "/__kosheros__/";
  var WORDS_URL = ROOT + "words.json";
  var JUDGE_URL = ROOT + "judge";
  var me = document.currentScript;
  var mode = (me && me.getAttribute("data-kosheros-language")) || "off";
  var judge = !!(me && me.getAttribute("data-kosheros-judge") === "1");
  var showImages = !!(me && me.getAttribute("data-kosheros-images") === "1");
  var token = (me && me.getAttribute("data-kosheros-token")) || "";
  if (me) me.removeAttribute("data-kosheros-token");
  if (mode === "off" && !judge && !showImages) return;
  if (window.__kosheros_page_filter) return;
  window.__kosheros_page_filter = true;

  // Status badges observe the existing image requests; they never change an
  // image URL, delay loading, or send image bytes back to the proxy.
  function startImageStatus() {
    var states = new Map(), visible = new Set(), pollTimer = null, polling = false, layoutQueued = false;
    var labels = { waiting: "Waiting…", checking: "Checking…", checked: "Checked",
      filtered: "Filtered", hidden: "Hidden", unavailable: "Not checked · hidden",
      not_checked: "Not checked", unknown: "Status unavailable" };
    var host = document.createElement("div");
    host.setAttribute("data-kosheros-ui", "images");
    host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483646";
    var shadow = host.attachShadow({ mode: "open" });
    var style = document.createElement("style");
    if (me && me.nonce) style.nonce = me.nonce;
    style.textContent = ":host{position:fixed;inset:0;pointer-events:none;z-index:2147483646}.badge{position:absolute;pointer-events:none;font:11px/1.4 system-ui,sans-serif;padding:2px 6px;border-radius:4px;background:rgba(22,28,35,.82);color:#fff;white-space:nowrap;box-shadow:0 1px 3px #0003}.badge[data-state=checking],.badge[data-state=waiting]{padding:0 4px;font-size:12px;line-height:14px;background:rgba(22,28,35,.45);box-shadow:none}";
    shadow.appendChild(style);
    document.documentElement.appendChild(host);

    function source(img) {
      // Inline backgrounds cover carousels without scanning computed styles
      // across every element on a busy page. Multiple layers stay unlabelled.
      var raw = img.currentSrc || img.src;
      if (!raw && img.style) {
        var match = /^url\(["']?([^"')]+)["']?\)$/.exec(img.style.backgroundImage);
        raw = match && match[1];
      }
      if (!raw) return "";
      try { var u = new URL(raw, location.href); return /^https?:$/.test(u.protocol) && u.href.length <= 2048 ? u.href.split("#")[0] : ""; }
      catch (e) { return ""; }
    }
    function forget(img) {
      var item = states.get(img);
      if (item) item.badge.remove();
      states.delete(img); visible.delete(img);
      intersection.unobserve(img); resize.unobserve(img);
    }
    function setState(item, state) {
      item.state = state;
      var pending = state === "waiting" || state === "checking";
      var label = labels[state] || labels.unknown;
      item.badge.textContent = pending ? "…" : label;
      item.badge.setAttribute("role", "img");
      item.badge.setAttribute("aria-label", label);
      item.badge.setAttribute("data-state", state);
      if (state === "checked" || state === "not_checked") item.badge.hidden = true;
    }
    function layout() {
      layoutQueued = false;
      visible.forEach(function (img) {
        var item = states.get(img);
        if (!img.isConnected) { forget(img); return; }
        if (item.state === "checked" || item.state === "not_checked") {
          item.badge.hidden = true; item.displayed = false; return;
        }
        var rect = img.getBoundingClientRect();
        var show = rect.width >= 80 && rect.height >= 48;
        item.displayed = show;
        item.badge.hidden = !show;
        if (show) item.badge.style.transform = "translate(" + Math.max(0, rect.left + 5) + "px," + Math.max(0, rect.top + 5) + "px)";
      });
    }
    function scheduleLayout() { if (!layoutQueued) { layoutQueued = true; requestAnimationFrame(layout); } }
    var intersection = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!states.has(entry.target)) return;
        if (entry.isIntersecting) visible.add(entry.target);
        else { visible.delete(entry.target); states.get(entry.target).badge.hidden = true; }
      });
      scheduleLayout(); schedulePoll();
    });
    var resize = new ResizeObserver(scheduleLayout);
    function refresh(img) {
      var item = states.get(img), url = source(img);
      if (!url) { if (item) forget(img); return; }
      if (!item) {
        var badge = document.createElement("span"); badge.className = "badge"; badge.hidden = true;
        shadow.appendChild(badge);
        item = { badge: badge, url: "", state: "waiting", started: 0, settled: false };
        states.set(img, item); intersection.observe(img); resize.observe(img);
      }
      if (item.url !== url) { item.url = url; item.started = 0; item.settled = false; setState(item, "waiting"); }
      scheduleLayout(); schedulePoll();
    }
    function scan(root) {
      if (!root || root.nodeType !== 1 || root.hasAttribute("data-kosheros-ui")) return;
      if (root.tagName === "IMG" || root.style.backgroundImage) refresh(root);
      root.querySelectorAll('img,[style*="background"]' ).forEach(refresh);
    }
    function schedulePoll() {
      if (!pollTimer && !polling && !document.hidden) pollTimer = setTimeout(poll, 750);
    }
    function poll() {
      pollTimer = null;
      var wanted = [], bytes = 20;
      visible.forEach(function (img) {
        var item = states.get(img);
        if (!item || !item.displayed || item.settled || wanted.length >= 64) return;
        if (item.started && Date.now() - item.started > 30000) { setState(item, "unknown"); item.settled = true; return; }
        var size = JSON.stringify(item.url).length + 1;
        if (bytes + size > 18000) return;
        bytes += size;
        if (!item.started) item.started = Date.now();
        wanted.push({ item: item, url: item.url });
      });
      if (!wanted.length || document.hidden) return;
      polling = true;
      var req = new XMLHttpRequest(); req.open("POST", ROOT + "image-status", true);
      req.setRequestHeader("Content-Type", "application/json");
      req.setRequestHeader("X-KosherOS-Page", token);
      req.timeout = 3000;
      req.onload = function () {
        var images = {}; try { if (req.status === 200) images = JSON.parse(req.responseText).images || {}; } catch (e) {}
        wanted.forEach(function (entry) {
          var item = entry.item, state = images[entry.url];
          if (item.url !== entry.url) return; // source changed during the request
          if (state && Object.prototype.hasOwnProperty.call(labels, state)) {
            setState(item, state); item.settled = state !== "checking";
          }
        });
        polling = false; scheduleLayout(); schedulePoll();
      };
      req.onerror = req.ontimeout = function () { polling = false; schedulePoll(); };
      req.send(JSON.stringify({ urls: wanted.map(function (entry) { return entry.url; }) }));
    }
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        if (r.type === "attributes") {
          if (r.target.hasAttribute("data-kosheros-ui")) return;
          if (r.target.tagName === "IMG" || r.attributeName === "style") refresh(r.target);
          else if (r.target.tagName === "SOURCE" && r.target.parentElement) scan(r.target.parentElement);
        } else {
          r.addedNodes.forEach(scan);
          r.removedNodes.forEach(function (root) {
            if (root.nodeType !== 1 || root.isConnected) return;
            if (states.has(root)) forget(root);
            root.querySelectorAll('img,[style*="background"]').forEach(function (img) { if (states.has(img)) forget(img); });
          });
        }
      });
    }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset", "sizes", "style"] });
    document.addEventListener("load", function (event) { if (event.target.tagName === "IMG") refresh(event.target); }, true);
    document.addEventListener("error", function (event) {
      var item = states.get(event.target);
      if (item) { setState(item, "unknown"); item.settled = true; scheduleLayout(); }
    }, true);
    addEventListener("scroll", scheduleLayout, { passive: true, capture: true });
    addEventListener("resize", scheduleLayout, { passive: true });
    document.addEventListener("visibilitychange", schedulePoll);
    scan(document.documentElement);
  }
  if (showImages && typeof IntersectionObserver !== "undefined" && typeof ResizeObserver !== "undefined") startImageStatus();
  if (mode === "off" && !judge) return;

  // Elements whose text is not prose: code the page runs or shows raw,
  // and what the person is typing (their own words are not the page's).
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, INPUT: 1, CODE: 1, PRE: 1, KBD: 1, SAMP: 1 };

  function skipped(node) {
    for (var el = node.parentNode; el && el.nodeType === 1; el = el.parentNode) {
      if (SKIP[el.nodeName] || el.hasAttribute("data-kosheros-ui")) return true;
      if (el.isContentEditable) return true;
    }
    return false;
  }

  function lettersIn(s) {
    var n = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      if (c.toLowerCase() !== c.toUpperCase() || /[֐-׿؀-ۿЀ-ӿ]/.test(c)) n++;
    }
    return n;
  }

  function matchCase(original, replacement) {
    var letters = original.replace(/[^A-Za-zÀ-ɏЀ-ӿ]/g, "");
    if (letters.length > 1 && letters === letters.toUpperCase() && letters !== letters.toLowerCase()) {
      return replacement.toUpperCase();
    }
    if (letters && letters.charAt(0) === letters.charAt(0).toUpperCase() &&
        letters.charAt(0) !== letters.charAt(0).toLowerCase()) {
      return replacement.charAt(0).toUpperCase() + replacement.slice(1);
    }
    return replacement;
  }

  // The word list as the proxy serves it: one pattern per script with a
  // single group, and for each word its own pattern and replacement, so a
  // hit (rare) can be named (see language.py, _matched_word).
  var lists = null;

  function compile(doc) {
    var patterns = [];
    for (var i = 0; i < doc.patterns.length; i++) {
      var p = doc.patterns[i];
      try {
        patterns.push({
          presence: p.presence ? new RegExp(p.presence) : null,
          re: new RegExp(p.pattern, "gi")
        });
      } catch (e) { /* a pattern this browser cannot take is skipped */ }
    }
    var words = [];
    for (var j = 0; j < doc.words.length; j++) {
      var w = doc.words[j];
      words.push({ word: w[0], source: w[1], replacement: w[2], re: null });
    }
    return { patterns: patterns, words: words, mode: mode };
  }

  function nameOf(found) {
    // Longest first, as the proxy orders them, so "bullshit" wins over
    // "shit"; each word's own pattern is compiled the first time a hit
    // is a candidate, not for every word up front.
    for (var i = 0; i < lists.words.length; i++) {
      var w = lists.words[i];
      if (!w.re) {
        try { w.re = new RegExp("^(?:" + w.source + ")$", "i"); }
        catch (e) { w.re = /$^/; }
      }
      if (w.re.test(found)) {
        // plausible(): at least half the word's letters are letters in the hit.
        if (lettersIn(found) * 2 >= lettersIn(w.word)) return w;
        return null;
      }
    }
    return null;
  }

  function clean(text) {
    var count = 0, hit = null;
    for (var i = 0; i < lists.patterns.length; i++) {
      var p = lists.patterns[i];
      if (p.presence && !p.presence.test(text)) continue;
      text = text.replace(p.re, function (whole, group) {
        var w = nameOf(group !== undefined ? group : whole);
        if (!w) return whole;
        count++;
        hit = hit || w.word;
        return mode === "substitute" ? matchCase(whole, w.replacement) : whole;
      });
    }
    return { text: text, count: count, hit: hit };
  }

  var blocked = false;

  function showBlockPage(html) {
    if (blocked) return;
    blocked = true;
    try {
      document.open();
      document.write(html);
      document.close();
    } catch (e) {
      document.documentElement.innerHTML = html;
    }
  }

  function ask(body) {
    // The proxy answers this on the page's own origin and never forwards it.
    var xhr = new XMLHttpRequest();
    xhr.open("POST", JUDGE_URL, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("X-KosherOS-Page", token);
    xhr.onload = function () {
      if (xhr.status === 403) { judge = false; return; }   // not this page's token
      if (xhr.status !== 200) return;
      try {
        var verdict = JSON.parse(xhr.responseText);
        if (verdict.blocked && verdict.page) showBlockPage(verdict.page);
      } catch (e) { /* not ours to break the page over */ }
    };
    xhr.send(JSON.stringify(body));
  }

  // Content judging: the words the page adds, gathered and sent in
  // batches rather than per node, with a ceiling so a page that never
  // stops rendering does not keep the proxy busy forever.
  var pending = [], pendingBytes = 0, sentBytes = 0, timer = null;
  var BATCH = 2000, MAX_BATCH = 100000, MAX_TOTAL = 600000;

  function gather(text) {
    if (!judge || blocked || sentBytes >= MAX_TOTAL) return;
    var t = text.replace(/\s+/g, " ").trim();
    if (t.length < 2) return;
    pending.push(t);
    pendingBytes += t.length;
    if (pendingBytes >= MAX_BATCH) flush();
    else if (!timer) timer = setTimeout(flush, BATCH);
  }

  function flush() {
    timer = null;
    if (!pending.length || blocked) return;
    var text = pending.join(" ");
    pending = []; pendingBytes = 0;
    sentBytes += text.length;
    ask({ url: location.href, text: text });
  }

  function handleText(node) {
    if (blocked || skipped(node)) return;
    var original = node.data;
    if (!original || !original.trim()) return;
    if (lists && mode !== "off") {
      var r = clean(original);
      if (r.hit && mode === "block") {
        ask({ url: location.href, language: r.hit });
        return;
      }
      if (r.count && r.text !== original) node.data = r.text;
    }
    gather(node.data);
  }

  function walk(root) {
    if (root.nodeType === 3) { handleText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 11) return;
    if (root.nodeType === 1 && (SKIP[root.nodeName] || root.isContentEditable || root.hasAttribute("data-kosheros-ui"))) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = walker.nextNode())) handleText(n);
  }

  var observer = new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      if (r.type === "characterData") handleText(r.target);
      else for (var j = 0; j < r.addedNodes.length; j++) walk(r.addedNodes[j]);
    }
  });

  function start() {
    // The HTML the proxy sent was already clean and judged; what is here
    // now beyond that was rendered by script, so it is looked at too.
    if (document.body) walk(document.body);
    observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  }

  if (mode === "off") {
    lists = { patterns: [], words: [] };
    start();
    return;
  }
  var req = new XMLHttpRequest();
  req.open("GET", WORDS_URL, true);
  req.setRequestHeader("X-KosherOS-Page", token);
  req.onload = function () {
    if (req.status !== 200) { lists = { patterns: [], words: [] }; start(); return; }
    try { lists = compile(JSON.parse(req.responseText)); }
    catch (e) { lists = { patterns: [], words: [] }; }
    start();
  };
  req.onerror = function () { lists = { patterns: [], words: [] }; start(); };
  req.send();
})();
