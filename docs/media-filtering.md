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

1. **Size threshold (free).** Under 6 KB an image is an icon, a spacer or a
   tracking pixel. It never reaches the model.
2. **Source (free).** If the page's domain is in a category this account
   blocks, its imagery goes with it. No model, and no chance of a model
   being wrong.
3. **Cache by content hash (~1 ms).** The same logo, banner and avatar come
   back on every page of a site, and a verdict is kept for a month.
4. **The detector (tens of ms).** NudeNet's ONNX model, about 5 MB, on the
   CPU, in a small thread pool with a 2 s deadline.

The detector returns **labelled regions**, not one score, which is what
makes the ladder meaningful: exposure maps to `nsfw`, covered-but-prominent
to `suggestive`, weaker signals to `immodest`. A detected face on its own
is never a finding — otherwise every photograph of a person trips the
filter, which is an off switch with extra steps rather than a filter.

**When it cannot look, it hides.** No model installed, inference timed out,
a corrupt image: the answer is to hide the picture, never to show it. The
null detector returns *nothing* rather than "clean" specifically so that
"looked and found nothing" cannot be confused with "could not look".

The honest position on `immodest`: it is genuinely weaker than the two
levels above it. The detector has no label for bare arms or bare legs, so
this level catches what the model can see and no more. A family that wants
tzniut enforced strictly should choose **hide all pictures**, which is the
only setting that needs no judgement and is therefore the only one that is
right every time. The sections below on a tzniut classifier are the plan
for closing that gap; nothing in them has been built.

## Video

Judged by where it comes from, not by what is in it. Decoding frames means
ffmpeg and a second or more per clip, for a stream the person is already
watching, on a machine that may have two cores.

So the levers that actually work are used instead: the source (a blocked
category), the page (scored by its words), the poster thumbnail (an image,
filtered like any other), and for YouTube the per-category and
approved-channel limits, which are precise because YouTube labels its own
videos. Beyond that, an account at `suggestive` or stricter gets no
open-web video at all — a video is pictures at thirty a second, and nothing
here can look inside one in time.

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

**What shipped:** option 1. `kosherd/imageedit.py` pixelates the detected
regions (grown by 12%, since a tight box leaves a fringe of exactly what it
was meant to cover) and leaves the rest of the picture alone, so the page
still works. If the picture cannot be edited — an unreadable format, no
Pillow — the whole image is hidden rather than passed through, which is the
one failure mode that would matter.

Inpainting stays where it belongs: an experiment on capable hardware with a
measured verdict, not a promise in the default path.



## Running on the family's own computer, including a slow one

Filtering happens on the device. No image and no browsing history leaves
the house, the filter keeps working without a subscription, and there is no
service to breach. The cost of that choice is that the accuracy ceiling is
whatever a CPU-only laptop can do — often an old one — so every layer has
to be designed for that machine rather than a developer's.

### Cheapest thing that can decide, first

Each stage only runs if the one before it could not settle the question:

| stage | cost per image | settles |
|---|---|---|
| category of the page's domain | none (already known) | most of the bad web |
| shopping department / page metadata | microseconds | most of the shopping case |
| size threshold | none | icons, spacers, tracking pixels |
| on-disk cache hit by content hash | ~1 ms | everything seen before |
| small NSFW classifier, quantised | 5–20 ms | the clear cases |
| detector (NudeNet-class) | 50–200 ms | only the borderline ones |

The last row is the one that hurts on weak hardware, which is exactly why
it runs last and rarely. A two-stage gate — skip the detector when the fast
model is confident the image is safe — is the difference between a usable
page and an unusable one.

### A time budget, and what happens when it runs out

Every page gets a budget. When an image cannot be judged inside it, the
answer is **hide the image**, not "let it through while we think". A slow
machine then degrades into a stricter filter rather than a broken browser
or a silent hole. The placeholder says the image was not checked, so the
person can ask for it rather than wonder.

### Profiles, chosen by measuring the machine

At setup the machine times a classification and picks a profile:

- **thorough** — detector on borderline images, full resolution;
- **balanced** — quantised classifier, detector only on strong signals;
- **light** — classifier only, smaller input, wider cache reuse;
- **images off** — for hardware that cannot keep up at all, `media_level`
  falls back to `all`, which needs no model and is never wrong.

That last profile matters: on a machine too slow to classify, hiding images
outright is *better* filtering than a classifier that times out, and it is
honest about what the computer can do.

### Caching

Keyed by the SHA-256 of the image bytes plus the model version, stored on
disk and shared by every user on the machine. Repeat views cost nothing,
which is most views. A weak machine benefits from this more than a fast
one, so the cache is sized generously rather than kept small.

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

This is worth building before any model: it is a few days of work per major
site, it is auditable, and it handles the case actually being asked about.

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
