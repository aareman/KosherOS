/* The front page's three moving parts. Loaded only by the home template.
 *
 * 1. A reveal as each section arrives, once, and never if the reader has
 *    asked for less motion.
 * 2. A slow drift on the painting while the hero scrolls away.
 * 3. The group chooser, which draws the same card the admin app draws:
 *    every line is one of four groups a family might make (the sample
 *    family's), not a mock-up, and the
 *    blue/amber dot means what it means in the app.
 */
(function () {
  "use strict";

  var calm = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* ---------------------------------------------------------- reveals */

  var revealed = document.querySelectorAll(".ko-reveal");
  if (!("IntersectionObserver" in window) || calm.matches) {
    revealed.forEach(function (el) { el.classList.add("is-in"); });
  } else {
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-in");
        watcher.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -12% 0px", threshold: 0.08 });
    revealed.forEach(function (el) { watcher.observe(el); });

    // A viewport tall enough to hold the whole page can never scroll, so
    // anything under the observer's bottom margin would stay hidden for
    // good. Show everything when there is nothing to scroll.
    var settle = function () {
      var doc = document.documentElement;
      if (doc.scrollHeight > window.innerHeight + 4) return;
      revealed.forEach(function (el) { el.classList.add("is-in"); });
    };
    window.addEventListener("load", settle);
    window.addEventListener("resize", settle);
    settle();
  }

  /* --------------------------------------------------------- parallax */

  var art = document.querySelector(".ko-hero__art img");
  if (art && !calm.matches) {
    var pending = false;
    var drift = function () {
      pending = false;
      var y = window.pageYOffset || document.documentElement.scrollTop;
      if (y > window.innerHeight * 1.4) return;
      art.style.setProperty("--par", (y * 0.18).toFixed(1) + "px");
    };
    window.addEventListener("scroll", function () {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(drift);
    }, { passive: true });
    drift();
  }

  /* ----------------------------------------------------- group chooser */

  // held: the filter is holding this line. open: it is not. Four groups a
  // family might make — the sample family's — not anything that ships.
  var GROUPS = {
    little: {
      name: "shmuli", initial: "S", mode: "Little ones",
      badge: "Approved sites only", badgeState: "held",
      who: "The youngest in the house, where the whole web is too much.",
      rows: {
        web: ["Only an approved list of sites", "held"],
        pictures: ["No pictures from the web", "held"],
        language: ["Bad language replaced", "held"],
        youtube: ["No YouTube", "held"],
        apps: ["Only apps a parent chose", "held"]
      }
    },
    kids: {
      name: "yosef", initial: "Y", mode: "Kids",
      badge: "Filtered internet", badgeState: "held",
      who: "School age. Also what an account in no group gets.",
      rows: {
        web: ["Adult, gambling, dating, social and video blocked", "held"],
        pictures: ["Immodest pictures hidden", "held"],
        language: ["Bad language replaced", "held"],
        youtube: ["Strict: entertainment, gaming, music and Shorts blocked", "held"],
        apps: ["Only apps a parent chose", "held"]
      }
    },
    teens: {
      name: "rivky", initial: "R", mode: "Teens",
      badge: "Filtered internet", badgeState: "held",
      who: "More of the web, the same guardrails.",
      rows: {
        web: ["Adult and gambling blocked, news and approved video allowed", "held"],
        pictures: ["Immodest pictures hidden", "held"],
        language: ["Bad language replaced", "held"],
        youtube: ["Moderate", "held"],
        apps: ["Can install approved apps", "open"]
      }
    },
    grownups: {
      name: "avi", initial: "A", mode: "Grown-ups",
      badge: "Filtered internet", badgeState: "held",
      who: "A grown-up who wants the filter on for themselves.",
      rows: {
        web: ["Adult content and filter bypasses blocked", "held"],
        pictures: ["Immodest pictures hidden", "held"],
        language: ["Left alone", "open"],
        youtube: ["Moderate", "held"],
        apps: ["Can install approved apps", "open"]
      }
    }
  };
  var AVATARS = {
    little: "#c4577a", kids: "#8e6fd4", teens: "#d97b3c", grownups: "#3aa3c4"
  };

  var chips = Array.prototype.slice.call(document.querySelectorAll(".ko-chip"));
  var card = document.querySelector(".ko-card");
  if (chips.length && card) {
    var field = function (name) { return card.querySelector('[data-field="' + name + '"]'); };

    var show = function (key, animate) {
      var group = GROUPS[key];
      if (!group) return;
      field("name").textContent = group.name;
      field("initial").textContent = group.initial;
      field("initial").style.background = AVATARS[key];
      field("mode").textContent = group.mode;
      field("who").textContent = group.who;
      var badge = field("badge");
      badge.textContent = group.badge;
      badge.setAttribute("data-state", group.badgeState === "open" ? "open" : "held");

      var i = 0;
      Object.keys(group.rows).forEach(function (row) {
        var el = card.querySelector('[data-row="' + row + '"]');
        if (!el) return;
        var value = group.rows[row];
        el.querySelector("dd").textContent = value[0];
        el.setAttribute("data-state", value[1]);
        if (!animate || calm.matches) return;
        el.classList.remove("is-new");
        void el.offsetWidth;                    // restart the keyframe
        el.style.setProperty("--i", (i * 0.035).toFixed(3) + "s");
        el.classList.add("is-new");
        i += 1;
      });
    };

    var choose = function (chip, animate) {
      chips.forEach(function (other) {
        other.setAttribute("aria-selected", String(other === chip));
        other.tabIndex = other === chip ? 0 : -1;
      });
      show(chip.dataset.group, animate);
    };

    chips.forEach(function (chip, index) {
      chip.addEventListener("click", function () { choose(chip, true); });
      chip.addEventListener("keydown", function (event) {
        var step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        var next = chips[(index + step + chips.length) % chips.length];
        next.focus();
        choose(next, true);
      });
    });

    var selected = chips.filter(function (chip) {
      return chip.getAttribute("aria-selected") === "true";
    })[0] || chips[0];
    choose(selected, false);
  }
})();
