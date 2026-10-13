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
 * reads nothing but the page.
 */
(function () {
  "use strict";
  var ROOT = "/__kosheros__/";
  var WORDS_URL = ROOT + "words.json";
  var JUDGE_URL = ROOT + "judge";
  var me = document.currentScript;
  var mode = (me && me.getAttribute("data-kosheros-language")) || "off";
  var judge = !!(me && me.getAttribute("data-kosheros-judge") === "1");
  if (mode === "off" && !judge) return;
  if (window.__kosheros_page_filter) return;
  window.__kosheros_page_filter = true;

  // Elements whose text is not prose: code the page runs or shows raw,
  // and what the person is typing (their own words are not the page's).
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, INPUT: 1, CODE: 1, PRE: 1, KBD: 1, SAMP: 1 };

  function skipped(node) {
    for (var el = node.parentNode; el && el.nodeType === 1; el = el.parentNode) {
      if (SKIP[el.nodeName]) return true;
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
    xhr.onload = function () {
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
    if (root.nodeType === 1 && (SKIP[root.nodeName] || root.isContentEditable)) return;
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
  req.onload = function () {
    if (req.status !== 200) { lists = { patterns: [], words: [] }; start(); return; }
    try { lists = compile(JSON.parse(req.responseText)); }
    catch (e) { lists = { patterns: [], words: [] }; }
    start();
  };
  req.onerror = function () { lists = { patterns: [], words: [] }; start(); };
  req.send();
})();
