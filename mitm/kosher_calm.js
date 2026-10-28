/* KosherOS calm filter: the calmer versions of the addictive sites (#179).
 *
 * x.com, reddit, YouTube and facebook cannot always be blocked wholesale —
 * an article links a tweet you need, a reddit post holds the answer to a
 * bug — but they are built to be bottomless: the feed never ends, the
 * sidebar recommends, the comments pull you down. This script, served by
 * the proxy on a reserved address and injected into their pages, makes
 * them finite in the page itself (they are single-page apps; what they
 * draw never passes the proxy as HTML):
 *
 *   - A feed page ends after a set number of items. The ads, the sidebar,
 *     the trending and the recommendations never show at all.
 *   - A direct link to a post renders as the post alone: no comments, no
 *     related videos, no recommendation rail.
 *   - When the account has a daily budget for the site, the script sends
 *     a heartbeat while a page of it is on screen; when the proxy says
 *     the budget is spent, the page is replaced with the proxy's own
 *     "enough for today" page.
 *
 * The proxy decides which sites are calmed and injects this into their
 * pages only; a login flow never gets it. Which page is a post and which
 * a feed is worked out here too, from the rules the proxy puts on the
 * tag, because a click inside these apps does not load a new page.
 */
(function () {
  "use strict";
  var ROOT = "/__kosheros__/";
  var BEAT_URL = ROOT + "calm-beat";
  var me = document.currentScript;
  if (!me) return;
  var site = me.getAttribute("data-kosheros-calm-site") || "";
  var mode = me.getAttribute("data-kosheros-calm-mode") || "feed";
  var items = parseInt(me.getAttribute("data-kosheros-calm-items"), 10);
  var remaining = parseInt(me.getAttribute("data-kosheros-calm-remaining"), 10);
  var token = me.getAttribute("data-kosheros-token") || "";
  var rules = {};
  try { rules = JSON.parse(me.getAttribute("data-kosheros-calm-rules") || "{}"); } catch (e) { rules = {}; }
  if (me) me.removeAttribute("data-kosheros-token");
  if (!site || window.__kosheros_calm) return;
  window.__kosheros_calm = true;
  if (!(items > 0)) items = 30;

  // What each site draws, by its own names. `pair` is what counts as one
  // feed item; `strip` is what a calmer page never shows; `single` is
  // what a post on its own also does without (comments, related, rails).
  // These names are the sites' internals and will drift; the worst that
  // happens then is a page that reads cleaner than needed, since the
  // proxy's own content filters still run underneath.
  var SITES = {
    x: {
      item: '[data-testid="cellInnerDiv"]',
      strip: [
        '[data-testid="sidebarColumn"]',
        '[data-testid="Trending"]',
        '[aria-label="Timeline: Trending now"]',
        '[data-testid="placementTracking"]',
        '[aria-label="Who to follow"]',
        '[data-testid="DMDrawer"]',
        '[data-testid="BottomBar"]'
      ],
      single: [
        '[data-testid="sidebarColumn"]',
        '[aria-label="Timeline: Conversation"]'
      ]
    },
    reddit: {
      item: 'shreddit-post, div[data-testid="post-container"]',
      strip: [
        'shreddit-sidebar',
        '#right-sidebar-container',
        '[data-testid="frontpage-sidebar"]',
        '#trending-posts-rail',
        'shreddit-ad-post',
        '[data-testid="promoted-post"]',
        'reddit-search-large',
        'faceplate-tracker[noun="trending"]'
      ],
      single: [
        'shreddit-comment-tree',
        '#comment-tree',
        '[data-testid="comment-tree"]',
        '#right-sidebar-container',
        'shreddit-sidebar'
      ]
    },
    youtube: {
      item: ('ytd-rich-item-renderer, ytd-video-renderer, '
             + 'ytd-grid-video-renderer'),
      strip: [
        '#secondary',
        '#comments',
        'ytd-comments',
        '#related',
        'ytd-watch-next-secondary-results-renderer',
        'ytd-merch-shelf-renderer',
        'ytd-donation-shelf-renderer',
        'ytd-display-ad-renderer',
        'ytd-promoted-sparkles-web-renderer',
        '#player-ads',
        '#masthead-ad',
        'ytd-banner-promo-renderer',
        'ytd-statement-banner-renderer',
        'ytd-continuation-item-renderer',
        'ytd-feed-filter-chip-bar-renderer'
      ],
      single: [
        '#secondary',
        '#comments',
        'ytd-comments',
        '#related'
      ]
    },
    facebook: {
      item: 'div[role="article"]',
      strip: [
        // The rail is a `div` on the site today and an `aside` on some of
        // its pages; the role is what both carry.
        '[role="complementary"]',
        '[data-pagelet="RightRail"]',
        '[data-pagelet="Stories"]',
        '[aria-label="Stories"]',
        '[data-pagelet^="Ad"]',
        '[aria-label="Reels and short videos"]'
      ],
      single: [
        '[role="complementary"]',
        '[aria-label="Comments"]',
        '[data-pagelet="RightRail"]'
      ]
    }
  };

  var spec = SITES[site] || {};
  var stopped = false;

  function pathNow() {
    return location.pathname || "/";
  }

  function modeNow() {
    var path = pathNow(), lower = path.toLowerCase(), i;
    var login = rules.login || [];
    var single = rules.single || [];
    if (!login.length && !single.length) return mode;  // no rules on the tag
    for (i = 0; i < login.length; i++) {
      if (lower.indexOf(String(login[i]).toLowerCase()) === 0) return "login";
    }
    for (i = 0; i < single.length; i++) {
      try { if (new RegExp(single[i]).test(path)) return "single"; }
      catch (e) { /* a pattern this browser cannot take is skipped */ }
    }
    return "feed";
  }

  function hide(el) {
    if (!el || el.nodeType !== 1 || el.__kosherosCalmHidden) return;
    el.__kosherosCalmHidden = true;
    el.style.setProperty("display", "none", "important");
  }

  function hideAll(root, selector) {
    var found;
    try { found = root.querySelectorAll(selector); }
    catch (e) { return; }
    for (var i = 0; i < found.length; i++) hide(found[i]);
  }

  function stripAll(root) {
    var list = spec.strip || [];
    for (var i = 0; i < list.length; i++) hideAll(root, list[i]);
  }

  function itemsIn() {
    if (!spec.item) return [];
    var found;
    try { found = document.querySelectorAll(spec.item); }
    catch (e) { return []; }
    // Only the ones that are a feed's own: a recommended video rail
    // carries the same element names as the feed, so leave out anything
    // inside a stripped part of the page.
    var out = [];
    for (var i = 0; i < found.length; i++) {
      var el = found[i];
      if (el.closest && el.closest('#secondary, #related, [role="complementary"], shreddit-sidebar')) continue;
      out.push(el);
    }
    return out;
  }

  function removeEndCard() {
    var old = document.getElementById("__kosheros_calm_end");
    if (old && old.parentNode) old.parentNode.removeChild(old);
  }

  function endCard(count) {
    var box = document.createElement("div");
    box.id = "__kosheros_calm_end";
    box.setAttribute("data-kosheros-calm-end", "1");
    box.style.cssText = ("margin:28px auto;padding:20px 24px;max-width:560px;"
      + "border:1px solid rgba(128,128,128,.35);border-radius:14px;"
      + "font:15px/1.5 system-ui,sans-serif;text-align:center;color:inherit;");
    var head = document.createElement("strong");
    head.textContent = "That is " + count + " items — the end of this feed.";
    var tail = document.createElement("div");
    tail.style.cssText = "margin-top:6px;opacity:.7;font-size:13px;";
    tail.textContent = "KosherOS ends these feeds on purpose. The site comes back fresh tomorrow.";
    box.appendChild(head);
    box.appendChild(tail);
    return box;
  }

  function capFeed() {
    var found = itemsIn();
    for (var i = 0; i < found.length; i++) {
      if (i >= items) hide(found[i]);
    }
    removeEndCard();
    if (found.length <= items) return;
    var last = found[items - 1];
    var card = endCard(Math.min(items, found.length));
    if (last && last.parentNode) last.parentNode.insertBefore(card, last.nextSibling);
  }

  // Same-site links in a post go nowhere, so a linked post has no rabbit
  // hole behind it; a link out (the reason somebody linked the post) still
  // works (#179). Each site sends its links out through an address of its
  // own, and that address counts as out: t.co on X is not listed as X's.
  var LINKS = {
    x: { own: ["x.com", "twitter.com"], out: [], outPaths: [] },
    reddit: { own: ["reddit.com", "redd.it"], out: ["out.reddit.com"], outPaths: [] },
    youtube: { own: ["youtube.com", "youtu.be"], out: [], outPaths: ["/redirect"] },
    facebook: { own: ["facebook.com", "fb.com", "fb.watch"],
                out: ["l.facebook.com", "lm.facebook.com"], outPaths: [] }
  };

  function hostIn(host, list) {
    for (var i = 0; i < list.length; i++) {
      if (host === list[i] || host.slice(-(list[i].length + 1)) === "." + list[i]) return true;
    }
    return false;
  }

  function inertLink(href) {
    var links = LINKS[site];
    if (!links || !href) return false;
    var url;
    try { url = new URL(href, location.href); } catch (e) { return false; }
    if (url.protocol !== "https:" && url.protocol !== "http:") return false;
    var host = url.hostname.toLowerCase();
    if (hostIn(host, links.out)) return false;
    if (!hostIn(host, links.own)) return false;
    for (var i = 0; i < links.outPaths.length; i++) {
      if (url.pathname === links.outPaths[i]) return false;
    }
    return true;
  }

  function onLinkClick(e) {
    if (stopped || modeNow() !== "single") return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || !inertLink(a.getAttribute("href"))) return;
    // Before the app's own router, which listens further down.
    e.preventDefault();
    e.stopImmediatePropagation();
  }

  function markLinks() {
    var anchors = document.querySelectorAll("a[href]");
    for (var i = 0; i < anchors.length; i++) {
      if (!inertLink(anchors[i].getAttribute("href"))) continue;
      anchors[i].setAttribute("data-kosheros-calm-inert", "");
      anchors[i].style.setProperty("cursor", "default", "important");
    }
  }

  function cleanSingle() {
    var list = spec.single || [];
    for (var i = 0; i < list.length; i++) hideAll(document, list[i]);
    removeEndCard();
    markLinks();
    // A conversation page keeps the first post and hides the rest of the
    // thread, so the linked post is the page (#179: "the post alone").
    if (spec.item) {
      var found = itemsIn();
      for (var j = 1; j < found.length; j++) hide(found[j]);
    }
  }

  var applyTimer = null;

  function apply() {
    if (stopped) return;
    var now = modeNow();
    if (now === "login") return;  // signing in is never touched
    stripAll(document);
    if (now === "single") cleanSingle();
    else capFeed();
  }

  function schedule() {
    if (applyTimer) return;
    applyTimer = setTimeout(function () {
      applyTimer = null;
      apply();
    }, 150);
  }

  // Single-page apps: a click changes the address without loading a page,
  // so the mode is worked out again whenever it does. The observer then
  // catches what the app draws next, and the debounce keeps a busy feed
  // from being walked on every frame.
  var lastURL = location.href;

  function checkURL() {
    if (location.href === lastURL) return;
    lastURL = location.href;
    removeEndCard();
    apply();
  }

  function hookHistory() {
    ["pushState", "replaceState"].forEach(function (name) {
      var original = history[name];
      if (typeof original !== "function") return;
      history[name] = function () {
        var result = original.apply(this, arguments);
        try { checkURL(); } catch (e) { /* never break the app's own routing */ }
        return result;
      };
    });
    window.addEventListener("popstate", function () { lastURL = ""; checkURL(); });
    window.addEventListener("hashchange", function () { lastURL = ""; checkURL(); });
  }

  var observer = new MutationObserver(function () { schedule(); });

  function start() {
    hookHistory();
    // Capture on the window, so this runs before the app's own handlers.
    window.addEventListener("click", onLinkClick, true);
    window.addEventListener("auxclick", onLinkClick, true);
    apply();
    observer.observe(document.documentElement,
                     { childList: true, subtree: true });
  }

  // ---- the daily budget -------------------------------------------------
  //
  // Seconds on screen, reported while the page is visible. The proxy
  // keeps the total and answers with what is left; it is the proxy that
  // decides the budget is spent, and its page that replaces this one. A
  // hidden tab counts for nothing, which is the point of "while it is on
  // screen".
  var BEAT_SECONDS = 15;
  var budgeted = remaining >= 0;
  var lastBeat = Date.now();
  var hidden = false;

  function stop() {
    stopped = true;
    if (observer) observer.disconnect();
    if (beatTimer) clearInterval(beatTimer);
  }

  function showPage(html) {
    try {
      document.open();
      document.write(html);
      document.close();
    } catch (e) {
      document.documentElement.innerHTML = html;
    }
  }

  function send(seconds) {
    if (!budgeted || stopped || seconds <= 0) return;
    var xhr = new XMLHttpRequest();
    xhr.open("POST", BEAT_URL, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("X-KosherOS-Page", token);
    xhr.onload = function () {
      if (xhr.status === 403) { stop(); return; }  // not this page's token
      if (xhr.status !== 200) return;
      try {
        var answer = JSON.parse(xhr.responseText);
        if (answer.over && answer.page) {
          stop();
          showPage(answer.page);
        }
      } catch (e) { /* not ours to break the page over */ }
    };
    xhr.send(JSON.stringify({ url: location.href, seconds: seconds }));
  }

  function beat() {
    var now = Date.now();
    var seconds = Math.round((now - lastBeat) / 1000);
    lastBeat = now;
    if (!budgeted || stopped || hidden) return;
    if (seconds > BEAT_SECONDS * 4) seconds = BEAT_SECONDS * 4;
    send(seconds);
  }

  var beatTimer = null;

  function watchBudget() {
    if (!budgeted) return;
    document.addEventListener("visibilitychange", function () {
      hidden = document.visibilityState === "hidden";
      lastBeat = Date.now();
    });
    window.addEventListener("pagehide", function () { beat(); });
    beatTimer = setInterval(beat, BEAT_SECONDS * 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      start();
      watchBudget();
    });
  } else {
    start();
    watchBudget();
  }
})();
