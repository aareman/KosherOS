#!/usr/bin/env python3
"""What the filter costs, on the machine it has to run on.

Local-first is the constraint every design decision here was made against,
and up to now the latencies in the docs were estimates. This measures
them. Run inside the built image:  just benchmark

Reports per-operation cost and the resident memory the proxy holds, which
is the number that matters on a machine with 4 GB.
"""

from __future__ import annotations

import io
import os
import statistics
import sys
import time


def rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def timed(label: str, fn, n: int = 20, warmup: int = 2) -> float:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    median = statistics.median(samples)
    worst = max(samples)
    print(f"  {label:<44} {median:7.2f} ms   (worst {worst:7.2f})")
    return median


def make_image(width: int, height: int) -> bytes:
    from PIL import Image

    # Noise, not a flat colour: a flat image compresses to nothing and
    # would flatter both the decoder and the model.
    data = os.urandom(width * height * 3)
    image = Image.frombytes("RGB", (width, height), data)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=80)
    return out.getvalue()


def main() -> int:
    print(f"python {sys.version.split()[0]}, {os.cpu_count()} cpus, "
          f"baseline RSS {rss_mb():.0f} MB\n")

    # -- text ---------------------------------------------------------------
    from kosherd import content, elementfilter, language, siterules

    scorer = content.load()
    page = ("<html><body>" + "<p>Chicken soup with kneidlach and matzah "
            "balls, a recipe for shabbos.</p>" * 400 + "</body></html>")
    print(f"text ({len(page) // 1024} KB page):")
    timed("content.visible_text", lambda: content.visible_text(page))
    text = content.visible_text(page)
    timed("content.score", lambda: scorer.score(text))
    wordlist = language.load()
    timed("language.contains_any", lambda: wordlist.contains_any(text))
    timed("language.clean_html", lambda: language.clean_html(page, wordlist))

    rules = siterules.load()
    spec = rules.strip_spec("www.amazon.com")
    nav = ("<html><body><ul>" + "".join(
        f"<li><a href='/b/d{i}'>Department {i}</a></li>" for i in range(60))
        + "<li><a href='/b/womens-lingerie'>Lingerie</a></li></ul>"
        + "<p>Results.</p>" * 2000 + "</body></html>")
    clean_nav = nav.replace("Lingerie", "Luggage").replace(
        "womens-lingerie", "womens-luggage")
    print(f"\nshop pages ({len(nav) // 1024} KB):")
    if spec:
        tags, is_blocked, quick = spec
        timed("elementfilter.strip (has a match)",
              lambda: elementfilter.strip(nav, is_blocked, tags, quick), n=10)
        timed("elementfilter.strip (quick-rejected)",
              lambda: elementfilter.strip(clean_nav, is_blocked, tags, quick))
    timed("siterules.reason",
          lambda: rules.reason("https://www.amazon.com/s?k=socks&i=aps"))

    # -- categories ---------------------------------------------------------
    from kosherd import categories

    bundle = categories.load_any()
    print(f"\ncategories ({len(bundle):,} domains):")
    hosts = ["chinuch.org", "pornhub.com", "a-domain-nobody-has.example",
             "www.amazon.com", "bet365.com"]
    timed("blocked_categories_of (5 hosts)",
          lambda: [bundle.blocked_categories_of(h, ["adult", "gambling"])
                   for h in hosts])
    print(f"  RSS after loading the category database   {rss_mb():7.0f} MB")

    # -- pictures -----------------------------------------------------------
    from kosherd import imageedit, vision

    print("\npictures:")
    sizes = [(320, 240), (800, 600), (1920, 1080)]
    images = {s: make_image(*s) for s in sizes}
    detector = vision.NudeNetDetector()
    if not detector.available:
        print("  no detector installed; skipping")
    else:
        for size in sizes:
            blob = images[size]
            timed(f"detect {size[0]}x{size[1]} ({len(blob) // 1024} KB)",
                  lambda b=blob: detector.detect(b), n=10, warmup=1)
        blob = images[(800, 600)]
        timed("imageedit.cover one region",
              lambda: imageedit.cover(blob, [(100, 100, 200, 200)]), n=10)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache = vision.VerdictCache(f"{tmp}/i.sqlite")
            f = vision.ImageFilter(detector=detector, cache=cache)
            f.verdict(blob)  # prime
            timed("ImageFilter.verdict (cache hit)", lambda: f.verdict(blob))
        print(f"  RSS with the model loaded                 {rss_mb():7.0f} MB")

    print(f"\npeak RSS {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
