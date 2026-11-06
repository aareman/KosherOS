# Local browser evidence for media filtering

This harness drives an isolated, **headed** Chromium/Brave profile on the device
through a separate local mitmproxy process. It does not change the system proxy,
account policy, browser profile, firewall, or power mode. Keep other test VMs and
builds stopped during measurement.

The fixture exercises the selected checkout's actual image/video response hooks,
SQLite verdict cache, image transforms and installed detector models. Authentication,
site categories and activity/status reporting are fixture-specific; no private
browsing data or account logs are read or written. All fixture origins bind to
loopback. Media is deterministic synthetic content, not a test of classification
accuracy or real websites' JavaScript.

## Run

Use a Python environment with `playwright`, `Pillow`, `av`, and the normal filter
runtime dependencies. No Playwright browser download is needed: connect to the
installed browser. `--mitmdump` must select a Python installation with the real
filter dependencies and models. On the test device this is `/usr/bin/mitmdump`.

Start a separate browser profile (leave your personal profile alone):

```sh
flatpak run com.brave.Browser \
  --user-data-dir=/tmp/kosher-browser-perf-isolated \
  --remote-debugging-port=9223 --no-first-run --no-default-browser-check \
  --disable-background-networking \
  --proxy-server=http://127.0.0.1:8087 '--proxy-bypass-list=<-loopback>' \
  about:blank
```

Export or check out the baseline revision in a separate directory, then run:

```sh
python scripts/perf-media/browser.py \
  --baseline /path/to/baseline --candidate /path/to/candidate \
  --candidate-pythonpath /path/to/candidate-pillow-wheel \
  --output /path/to/new-results-directory --seconds 20 --repeats 3
```

The optional candidate Python path reproduces an image-runtime dependency change
without installing packages on the host. This PR's browser captures use the same
Pillow 13 wheel selected by its Containerfile, with native AVIF support. The
baseline keeps the installed host Pillow. Source and runtime versions are saved
with every run.

Do not edit the harness or candidate during a run. Mitmproxy can hot-reload its
addon when it changes, invalidating a comparison. Runs alternate order. Each
trial uses a fresh proxy and verdict cache; browser HTTP caching is disabled.
Model/decoder startup is warmed before the inference and cover scenarios. One
warmup verdict is removed before inference measurement.

## Scenarios and checks

- **hidden:** 36 image requests, 24 distinct dimensions, forced hiding. Measures
  cold placeholder generation and verifies every image's dimensions and pixels.
- **intrinsic:** six hidden images whose layout depends on their actual aspect
  ratio. Catches a fast but incorrect 1×1 fallback hidden by fixed CSS dimensions.
- **covers:** cached synthetic verdicts force real frost rendering. Pixel checks
  verify the cover was actually applied; this does not measure model accuracy.
- **inference:** cold image verdict cache, real installed detection models,
  repeated images, and two H.264 clips. Checked/hidden/refused counts are recorded
  so a speedup cannot silently be attributed to disabling the filter. Geometry
  remains checked even when overload legitimately hides an image.

The page records navigation/critical-script timings, frame gaps while loading,
long tasks, layout shifts, and same-origin fetch latency while CDP sends clicks
and wheel events. Event Timing reports the browser's observed interaction
latency. Event-to-next-rAF and CDP round-trip times are separate diagnostics;
neither is called INP or guaranteed pixel presentation. Events below the
browser's reporting threshold are absent. A 20-second run is a controlled lab
sample, not a field Core Web Vitals assessment.

The proxy also records event-loop lag, detector calls and response decisions.
The candidate's production status script and API run during measurement, so
polling and badge rendering costs are included. Visible labels must settle.
Correctness failures make the command exit nonzero after preserving artifacts.
Brave's canvas privacy noise is accommodated with ±2 RGB tolerance for otherwise
uniform tiles; alpha and dimensions remain strict. Expected candidate colours
are calculated from the source assets before timed trials. Cover counts must
also be compared: hiding more pictures is not evidence of faster covering.

Each trial saves `browser.json`, `proxy.json`, `summary.json`, `browser.png`, and
`proxy.log`. Environment records contain source/fixture hashes, browser command,
hardware, kernel, load, and available power-mode information. Inspect raw data,
all repetitions and failure counts before interpreting any timing difference.

## Functional browser checks

`status_ui.py --output /path/to/results` connects to the same isolated browser
and intercepts only a `.test` fixture. It exercises the production script's
pending/finished labels, source changes, inline backgrounds, layout, clicks,
DOM removal, long signed URLs and images first shown after a delay.

`amazon.py --root /path/to/candidate --output /path/to/results` starts a separate
headed profile on port 9224 through a separate proxy on 8088. It uses a temporary
test CA pin confined to that profile and retains upstream TLS verification. It
does not use personal cookies, sign in, submit searches or make purchases.
`--extra-pythonpath` selects the candidate runtime. `--asset-list` accepts the
captured public carousel asset metadata; `--require-image-status` makes those
controlled asset pages fail unless the production status UI works. Default live
URLs are Amazon and Prime Video; `--urls` can include YouTube.

Live sites are functional evidence, not causal performance A/B tests: content
changes, network conditions vary, and this device's host filter can transform
responses before the test proxy sees them. Incoming filter headers are recorded
to identify that limitation. Controlled public-asset pages and the loopback
benchmark avoid it. Public-asset fixtures include one deliberately malformed
YouTube preview that exercises the production fallback; it is explicitly
synthetic and does not establish how often real YouTube previews fail.
