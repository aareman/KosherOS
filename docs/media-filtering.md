# Image and video filtering

Text filtering is the easy half. What a family notices is imagery: an
allowed news site with an immodest photo, a shopping site's lingerie
advertising, a thumbnail on a permitted video page. This is what separates
a filter people trust from one they route around.

## The levels

Per user, in filtered mode only (judging a picture means holding it, which
means reading the connection):

| level | what is hidden |
|---|---|
| `none` | nothing |
| `nsfw` | explicit imagery |
| `suggestive` | explicit and suggestive |
| `immodest` | also immodest imagery — the frum default |
| `all` | every remote image; pages render as text |

`all` is the only level that needs no judgement, so it is the only one that
is exactly right every time. Everything below it depends on a classifier
and will be wrong sometimes in both directions.

## How each level is actually enforced

**`all`** — the proxy replaces any image response with a placeholder. Cheap,
instant, completely reliable. Also genuinely usable: many sites remain
readable, and a person who needs a picture can ask for the page.

**`nsfw` / `suggestive` / `immodest`** — built, in `kosherd/vision.py`, in
this order, each stage running only because the one before it could not
settle the question:

1. **Size threshold (free).** Under 2.5 KB an image is an icon, a spacer or
   a tracking pixel. It never reaches the model. (It was 6 KB; modern
   WebP/AVIF thumbnails fit a lot of person into very few bytes.)
2. **Source (free).** If the page's domain is in a category this account
   blocks, its imagery goes with it. No model, and no chance of a model
   being wrong.
3. **Cache by content hash (~1 ms).** The same logo, banner and avatar come
   back on every page of a site, and a verdict is kept for a month.
4. **The detector (tens of ms).** NudeNet's ONNX model, about 5 MB, on the
   CPU, in a small thread pool with a 2 s deadline — and **awaited, not
   waited for**. The proxy runs its hooks on one asyncio event loop; the
   first version blocked that loop while a picture was judged, so every
   other connection on the machine stood still for up to two seconds per
   picture, and a page with thirty photographs froze the browser for the
   lot of them. That was most of "the system seems slow". The response
   hook is now `async`: the picture being judged waits on a worker, the
   rest of the machine's traffic carries on, and a worker that outlives
   the deadline still caches its verdict for the next time.

The detector returns **labelled regions**, not one score, which is what
makes the ladder meaningful: exposure maps to `nsfw`, covered-but-prominent
to `suggestive`, weaker signals to `immodest`. A detected face on its own
is never a finding — otherwise every photograph of a person trips the
filter, which is an off switch with extra steps rather than a filter.

**When it cannot look, it hides.** No model installed, inference timed out,
a corrupt image: the answer is to hide the picture, never to show it. The
null detector returns *nothing* rather than "clean" specifically so that
"looked and found nothing" cannot be confused with "could not look".

### The immodest level, and the one honest way to strengthen it

`immodest` is genuinely weaker than the two levels above it. The detector
has no label for a bare arm or a bare leg, so a clothed model in a
lingerie catalogue comes back **clean** and the picture stays on screen
next to the word "lingerie".

Rather than invent a signal, two that already exist are combined: what the
page says, and whether there is a person in the picture at all. The
detector is reliable about finding people and unreliable about judging
modesty, which is exactly why those are used differently. So a picture is
hidden when the page it came from scored at or beyond this account's
tolerance **and** the picture contains a person.

Neither half would do alone. Hiding every picture on a page that scored
immodest takes out the shop's own logo and navigation icons; hiding every
picture containing a person takes out a news photograph on a page about
nothing in particular. Both cases are tested.

The picture is matched to its page through the `Referer` header, against a
bounded cache of recent page verdicts — a browser fetches a page's images
within seconds of the page.

This closes most of the catalogue case and none of the general one. A
family that wants tzniut enforced strictly should still choose **hide all
pictures**, which needs no judgement and is therefore the only setting
that is right every time.

### Measuring skin over a figure

Hands-on family testing then found the misses the labels cannot see —
beachwear, bare shoulders and arms, legs under a short skirt — and the
level was strengthened with a skin measurement, used with care: never
over a whole picture, only over a **figure**. The whole body is estimated
from a detected face (or from the union of detected parts when there is
no face), and the fraction of skin-toned pixels in that box, by the
classic YCbCr gate, promotes a "clean" picture to immodest past
`SKIN_LIMIT`. Legs are measured on their own as well (`LEGS_SKIN_LIMIT`,
the lower middle of the body box), because a covered top dilutes bare legs
below the whole-figure line and short skirts were getting through.

A skirt photographed from the hips down has no face and no labelled part,
so none of that ran and the picture passed — on Amazon's own grid for
that search, eleven of sixty-two. A skin rule with no figure to anchor it
was measured and rejected: against a hundred and fifty beige products,
any threshold that caught most of the legs hid three quarters of the
furniture. What was missing was "is there a person here", so a **person
detector** answers it: YOLOX-Nano (`kosherd/persons.py`, Apache-2.0,
3.5 MB, about forty milliseconds), asked only when the nudity model found
no face and the picture has enough skin-toned area to matter. Its box is
what the skin measurement then runs on. On that grid it recovered ten of
the eleven.

Two limits of the colour gate are known and stated rather than hidden.
It sees some skin tones better than others, so it is stricter with some
families than others; that is why it is only ever applied inside a
detected figure, never used to *find* one. And khaki reads as skin to it:
of twenty-five photographs of men modelling khaki trousers, six are
hidden at this level. The sections below on a tzniut classifier remain
the plan for the rest.

## Video

Judged by its source first, then by a few of its frames.

The cheap levers still come first: the source (a blocked category), the
page (scored by its words), the poster thumbnail (an image, filtered like
any other), and for YouTube the per-category and approved-channel limits,
which are precise because YouTube labels its own videos.

Then, for an account whose pictures are filtered, the clip itself
(`kosherd/videocheck.py`). A video is pictures at thirty a second, and
decoding all of them is out of the question on two cores — but it never
needed all of them. Four keyframes spread through the clip, decoded small
(480 px on the short side, which is what the detector wants anyway), go
through the same detector and the same ladder as a photograph; the
strongest frame is the clip's level. The verdict is cached by address and
size for a month, so the cost — a decode plus four detections, one to two
seconds on two cores — is paid once per clip, not once per view. Decoding
is PyAV (FFmpeg's libraries as a wheel, with the H.264, VP9 and AV1
decoders Fedora's own `ffmpeg-free` leaves out).

The rules, in order, decided **from the headers before a byte of the body
is fetched** (`responseheaders`):

| situation | mildest level (`nsfw`) | modesty levels | `all` |
|---|---|---|---|
| source in a blocked category | refused | refused | refused |
| verdict already cached | as it says | as it says | refused |
| clip small enough to hold (≤ 40 MB), first request | held, sampled, then passed or refused | same | refused |
| too big to hold, or a range from the middle with no verdict yet | passed (as before) | refused (as before) | refused |
| no decoder on this machine | passed (as before) | refused (as before) | refused |
| could not be read, or missed the 8 s deadline | refused | refused | refused |

Two things to know. **Large files are not held.** Long-form video on the
web is almost always segmented (HLS/DASH); the manifest is text that points
at segments, and each segment is small and judged on its own, so a stream
is checked piece by piece. A single progressive file over the cap cannot be
sampled without holding it, so it keeps the old behaviour at each level.
**Frames are sampled, not watched.** Four frames from a two-minute clip can
miss a second of anything; this judges what a video is about. The guarantee
is still `all`, and the mildest level still lets through what it cannot
check, because it did before this existed and a missing decoder must not
take video away from an account that never asked for it to be checked.

A refused clip is a `403` with `x-kosheros: video-blocked`, sent in place
of the headers so the body is never downloaded; a passed one carries
`x-kosheros: video-checked=<level>`.

## Inpainting people out of images

This was asked for directly, so here is a straight answer.

**Technically possible.** Person segmentation (YOLOv8-seg, ~30 ms) plus
inpainting (LaMa, ~1–3 s for 512px on CPU) would remove a figure and fill
the background plausibly.

**Not viable inline, today.** At 1–3 seconds per image on CPU, a page with
twenty photographs stalls for the better part of a minute, and the proxy
holds the whole response while it works. On a GPU it is fast enough, but a
family laptop is exactly the machine that does not have one. It would also
mean shipping ~500 MB of model weights in the image.

**What to do instead**, in order of what actually helps:

1. **Blur** the offending region — same detection cost, no generation cost,
   and it preserves page layout so the site stays usable. This is what
   commercial filters do, and it is honest about what happened.
2. **Replace** the image with a neutral placeholder (what `all` does).
3. **Inpaint** only where it is worth waiting: an explicit, per-user opt-in
   on capable hardware, or done ahead of time for a small set of frequently
   visited pages. Worth prototyping; not worth putting in the default path.

**What shipped:** option 1, in `kosherd/imageedit.py`, shaped by four
rounds of family feedback. Tight pixel blocks left a fringe; blocks plus
static destroyed the content but shouted about it; a heavy blur with noise
under a feathered edge sat quietly in the picture but — the fourth round —
"blur is not so effective in blocking images": at any radius that keeps a
page fast, a figure's silhouette and skin tone stayed readable. The cover
now **frosts** the region: it is shrunk to a handful of cells, smoothed
back up, pulled a third of the way toward its own average colour, given
light noise (so it cannot be undone by deconvolution) and pasted through a
feathered mask. The shape is gone in the first step; the rest is making it
sit quietly. It is also cheaper than the blur it replaced, since the blur
now runs on a thumbnail. Regions are grown by 60% (the detector's boxes
are tight), and when the covered area would be most of the picture the
picture is hidden whole instead — a frosted rectangle in a frame of
background helps nobody and costs a decode and a re-encode.

Two costs fell out at the same time. A covered WebP came back as PNG,
five to ten times the bytes and a slow encode; each format now comes back
as itself. And the covered pixels are remembered for a while (a small
in-memory cache by content hash), so a reload or the same photo on the next
page is not decoded, frosted and re-encoded again — the verdict cache on
disk remembers the judgement; this only remembers the pixels.

If the picture cannot be edited — an unreadable format, no Pillow — the
whole image is hidden rather than passed through, which is the one failure
mode that would matter.

Inpainting stays where it belongs: an experiment on capable hardware with a
measured verdict, not a promise in the default path.



## Running on the family's own computer, including a slow one

Filtering happens on the device. No image and no browsing history leaves
the house, the filter keeps working without a subscription, and there is no
service to breach. The cost of that choice is that the accuracy ceiling is
whatever a CPU-only laptop can do — often an old one — so every layer has
to be designed for that machine rather than a developer's.

### Measured, on the machine that matters

`just benchmark` runs these inside the built image; `just benchmark 2`
runs them on two cores, which is the machine this product is for. The
numbers below are the two-core ones, because "it is fast on the
developer's laptop" is not the claim being made.

| operation | per call |
|---|---|
| category lookup | 0.004 ms |
| `siterules.reason` on a URL | under 0.01 ms |
| element strip, page has none of the words | 0.4 ms |
| content score, 28 KB page | 6.7 ms |
| profanity scan, 28 KB page | 7.3 ms |
| element strip with a match, 31 KB page | 6.3 ms |
| image verdict, cache hit | 0.2 ms |
| **detection, 800×600** | **440 ms** |
| resident memory, everything loaded | 150 MB |

Two things came out of measuring rather than estimating.

**The profanity scan was ten times more expensive than it needed to be.**
It used one *named* capture group per word, so the engine tracked 145 sets
of group boundaries through every character of every page: 40 ms on a
28 KB page against 4 ms for the identical pattern without them, paid
whether or not anything matched. The word that hit is now recovered
afterwards by testing the few characters that matched, which costs
something only when there is something to cost. There is a test asserting
the page-scanning pattern keeps exactly one group.

**Detection is 200–600 ms per image on two cores**, and no amount of
thread tuning moves it (constraining `OMP_NUM_THREADS` made it worse). At
that rate a news page with thirty photographs is fifteen seconds of
waiting — for a *half-checked* page, since many would hit the timeout and
be hidden anyway. That is worse for the person than the setting that needs
no model at all.

So the machine is measured instead of assumed. `ImageFilter` keeps a
rolling median of its own detection time, and above 400 ms it stops trying
and hides pictures instead: instant, never wrong, and honest about what
the computer can do. Verdicts already in the cache are still served, since
they cost nothing and are just as accurate on a slow machine. The window
rolls, so a machine that was briefly busy recovers on its own.

**And it says so.** The proxy publishes its state — `checking`, `too_slow`
or `no_model` — and kosherd reports it, with which services should be
running and are not, through `FilterStatus`. The admin app shows it as an
amber count beside Protection in the sidebar and spells it out first on the
Protection page, and `kosherctl status` prints it.

This matters more than it looks. A filter that has quietly stopped doing
something is worse than one that never did it, because the family is
relying on it. And the version of this feature that says nothing teaches
people that blank pictures mean the computer is broken — which is how a
filter ends up switched off.

### Would another language be faster? Mostly no

Asked seriously, and the table above answers it. Judging a page costs
about 20 ms of Python across scoring, profanity and element stripping.
Judging one picture costs 440 ms. So the entire Python text path is under
5% of the cost of a *single* image, and a page with a dozen pictures
makes it a rounding error.

The expensive parts are already not Python. Detection is ONNX Runtime,
which is C++; the category database is SQLite, which is C, and answers in
0.004 ms; image decoding and covering are Pillow, which is C. Rewriting
the parts that *are* Python — `content`, `language`, `elementfilter` — in
Rust or Go might recover 15 ms per page and would cost the property that
makes this project maintainable by one person with occasional
contributors.

Where the wins actually are, in order:

1. **The model.** Input resolution, int8 quantisation, and a smaller or
   better architecture all move the 440 ms. Nothing else does.
2. **Not running it.** The staged design, the content-hash cache and the
   `too_slow` fallback are all worth more than any language change.
3. **Algorithms inside Python.** The profanity scan above went from 40 ms
   to 4 ms by removing named capture groups — a ten-fold win from
   measuring, not from a rewrite. If the text path ever does need to be
   faster, C-extension libraries get most of it without leaving Python:
   `pyahocorasick` for multi-word matching, `selectolax` or `lxml` in
   place of `html.parser`.

The one honest candidate for another language is not per-operation
latency at all. It is **proxy throughput under concurrency** — mitmproxy
is async Python, and a browser opens many connections at once, which is a
different measurement from the ones in the table and has not been taken
yet. Take it before considering a rewrite; and the other reason to want a
compiled component, a single static binary for the standalone package in
[standalone.md](standalone.md), is a distribution argument rather than a
performance one.

### Cheapest thing that can decide, first

Each stage only runs if the one before it could not settle the question:

| stage | settles |
|---|---|
| category of the page's domain | most of the bad web, for free |
| department rules on the address | the shopping case, for free |
| size threshold | icons, spacers, tracking pixels |
| on-disk cache by content hash | everything seen before |
| the detector | only what is left |

### Pictures that move

A GIF, an animated PNG or an animated WebP is a short clip, and is treated
as one: `vision.animation_frames` takes four frames spread through the
animation and the strongest frame's verdict is the picture's. Before this,
a GIF was always hidden — the detector's image reader (OpenCV) cannot open
one, so every GIF was "could not judge" — and an animated PNG or WebP was
judged on its first frame alone, so a clean opening frame let the rest
through. Anything OpenCV cannot read (GIF, AVIF) is now handed to the
detector as a JPEG (`vision.prepare`), so a still GIF is judged like any
other picture. An animation that must be covered is hidden whole: a cover
placed on one frame means nothing on the others. WebM is video and takes
the video path.

### What is held and what streams

The proxy used to buffer every response body before the filter ran —
including a 200 MB download and every video at every media level. Only what
the filter reads is held now: pages, pictures for an account that filters
them, the JSON of a few known sites, and clips small enough to sample.
Downloads, archives, fonts, audio, anything large that is none of those,
and a connection that belongs to no filtered account go straight through
as they arrive. This is the other half of "the system seems slow": memory,
and a download that only started reaching the browser once the proxy had
all of it.

### Caching

Keyed by the SHA-256 of the image bytes plus the model version, stored on
disk and shared by every user on the machine. Repeat views cost nothing,
which is most views. A weak machine benefits from this more than a fast
one, so the cache is sized generously rather than kept small. Clip verdicts
live in the same file, keyed by address and size.

Deliberately no Redis on the device: another daemon holding RAM on a
machine already short of it, to cache something SQLite handles.

## Training a tzniut classifier: how hard, really

Two problems get conflated here, and only one of them needs a model.

### Shopping sites need text, not vision

Blocking women's clothing on Amazon is a **metadata** problem. The
department is in the URL and the page: a search carries an `i=fashion-womens`
style department parameter, a product page carries breadcrumbs and a
department field, a category page carries a node id. Reading that costs
microseconds, is exact, and never mistakes a sofa for a person.

Running a vision model over 40 product thumbnails to discover what the page
already says in text would be slower, less accurate, and pointless. So:
**department and category rules first**, vision only for imagery on pages
that carry no such labels. The same applies to most large sites worth
worrying about — they are databases with categories, and the categories are
in the markup.

**Built**, in `kosherd/siterules.py` and
`/usr/share/kosher/site-rules.json`. Rules describe a *part* of a site the
family uses — Amazon is not a site a family blocks, a department on it is —
and they fire only for accounts that block the `immodest` category, the
same setting that blocks lingerie retailers outright. They are matched on
the REQUEST, before a byte of the page is fetched, and in search results
too, so a result cannot lead somewhere a click would be blocked.

The reason names the term and the host ("the fashion-womens department of
amazon.com"), because an admin has to be able to disagree with a specific
word rather than with a probability.

### The site's own search box and autocomplete

Blocking the department is worth nothing if the search box on the same
page reaches it anyway, and autocomplete is worse than the results: it
puts the words on screen unprompted, while somebody is typing something
else. So both are filtered.

A search typed into a shop's own box is matched against the same
department vocabulary the address rules use (`?k=lingerie` on Amazon,
`?_nkw=bikini` on eBay) plus the explicit search blocklist. `?k=laptop
stand` and `?_nkw=sefer torah` go through untouched.

Autocomplete comes back as JSON, and every site nests it differently —
Amazon under `suggestions`, eBay under `res.sug`, others as a bare array.
Rather than learn each schema and re-learn it when it changes,
`kosherd/suggest.py` walks whatever came back and drops **list entries**
whose text is blocked, leaving the structure alone. Dropping entries from
a list of suggestions is the right edit and the one edit that cannot break
a response's shape. A suggestion is a few words rather than a page, so the
term list answers there and not the content scorer, which needs more
evidence than one word can carry.

### Image search

Search thumbnails were withheld outright from any account that filters
pictures. That is now conditional on whether they can be checked: in
filtered mode `image_proxy` is off, so each thumbnail loads from its
origin through the proxy and is judged like any other picture. Only the
modes that never read the connection still withhold them, because there
they would arrive unexamined.

### Removing the item beats judging the page

A shop names its whole catalogue in the navigation of every page. Scoring
that gives two bad answers and no good one: judge it strictly and Amazon
is blocked outright, judge it loosely and the sidebar stays on screen.

So on a covered site the offending items are removed first, and what is
left is judged normally. `kosherd/elementfilter.py` walks the document
with the standard library's `html.parser`, drops the `<a>`, `<li>` or
`<option>` elements whose text or address matches the site's department
vocabulary, and leaves everything else **byte-identical** — entities,
comments, script bodies, attribute quoting and all. That last part is the
constraint the whole design serves: a filter that rewrites markup it does
not understand breaks sites in ways nobody can debug. On any parse trouble
the original page is returned untouched.

A site can override both halves in its rules — `strip_tags` and
`strip_terms` — so a layout the defaults do not reach is a rules-file edit
rather than a release.

**What makes it affordable:** reparsing a megabyte of HTML costs about a
fifth of a second on the machine this has to run on. A plain substring
scan runs first and skips the parse entirely unless one of the words is
present, which takes a 0.6 MB clean page from 83 ms to 2 ms. Pages above
2 MB are not rewritten at all.

**When a page is too large or fails to parse**, nothing was removed, so
the catalogue vocabulary is still in the text and the scorer's floor drops
to *suggestive* for that page — otherwise a page about socks would be
convicted by a sidebar nobody could take out.

### Three rules keep this from becoming an outage

A site with its own rules is *not* also judged by the generic list, so
tuning one shop cannot silently widen another. The generic list only fires
on shop-shaped addresses (`/shop/`, `/collections/`, `/category/`),
because an article whose title contains the word is not a department.

And the vocabulary is checked against what people actually need. "Socks &
Hosiery" is where you buy socks, so `hosiery` is not a term — the tights
department says `tights` or `stockings`, and those are. Every term that
removes something a family needs is an outage with extra steps, and the
test suite asserts the sock department survives.

### The vision model, when it is genuinely needed

**Do not try to train "is this immodest" directly.** The blocker is not
compute, it is the training data: a supervised classifier needs tens of
thousands of labelled examples, and assembling and labelling a corpus of
immodest imagery is exactly the work a frum organisation cannot ask anyone
to do. Any plan that starts with "collect and label 50,000 images" is dead
on arrival for reasons that have nothing to do with engineering.

**Two paths avoid that entirely:**

**1. Geometry over pose and segmentation (recommended).** Off-the-shelf
person-keypoint and clothing-segmentation models — trained on ordinary
photographs, no problematic data involved — give joint positions and which
pixels are skin versus fabric. Halachic criteria are then expressed as
*rules* over that: are the elbows covered, the knees, the collarbone. The
rules are readable, a rav can review them, and they can differ per family
without retraining anything. That is a real advantage over a black box:
"the model decided" is not an answer a posek can work with.

**2. Fashion attribute datasets.** DeepFashion and Fashionpedia are
academic datasets labelled with sleeve length, neckline, hem length and so
on — the exact vocabulary tzniut cares about, already annotated, with no
objectionable content to handle. Fine-tuning a small classifier on those
attributes gets a model that reports "sleeveless, above-knee" rather than a
single opaque score.

### Speed

Speed is the easy part. A MobileNetV3 or EfficientNet-lite classifier at
224px runs in roughly 5–20 ms per image on a modern CPU, and INT8
quantisation takes another 2–4× off. Pose estimation (YOLOv8n-pose class)
is nearer 20–40 ms. Combined with:

- **skipping small images** (icons, spacers) — already implemented;
- **caching by content hash**, so a repeat view costs nothing;
- **the text rules above**, which resolve most shopping pages before an
  image is ever decoded;

a photo-heavy page lands in the low hundreds of milliseconds. That is a
fundamentally different proposition from inpainting at 1–3 seconds *per
image*.

### Honest effort estimate

| piece | effort | needs training data? |
|---|---|---|
| Department/category rules for major shopping sites | days per site | no |
| Pose + segmentation with halachic rules | weeks | no |
| Fine-tune on fashion attributes | weeks, plus evaluation | public datasets only |
| Train "immodest" end to end | months | yes — and the labelling is the blocker |
| Inpainting instead of blur | weeks | no, but too slow inline |

The order that gets the most filtering per unit of effort: shopping
metadata rules, then blur driven by pose geometry, then a fashion-attribute
classifier to sharpen it. A bespoke end-to-end model is the last thing to
reach for, not the first.

### One thing to decide early

Every approach above judges images **on the family's own machine**, which
is the right place for it — no photograph leaves the house and no third
party sees what anyone browses. It also means the accuracy ceiling is set
by what a laptop CPU can do. Sending images to a service would be faster
and better, and should be rejected for exactly the reason it is tempting.

## YouTube

YouTube is enough of the web on its own to deserve separate settings, and
Restricted Mode alone is far too coarse for a family that wants shiurim but
not entertainment.

- **Restricted Mode** (`none` / `moderate` / `strict`) is forced by DNS and
  by a request header — already working.
- **Category limits.** Every video carries a category id (Music, Gaming,
  News, Education…). The proxy can read it from the watch page and block
  the ones an admin has turned off.
- **Channel allow-list.** When set, only those channels play. This is the
  setting most frum families actually want: a handful of approved channels
  and nothing else, which is far stronger than any category rule.
- **Thumbnails** are images and fall under the media level above, which is
  why the two features belong together: allowing YouTube while blocking its
  thumbnails is a coherent, and popular, choice.
