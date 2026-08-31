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

**`nsfw` / `suggestive` / `immodest`** — these need a classifier, and the
cost lands per image on the family's own laptop:

1. **Source-based first (free).** If the page's domain is in a blocked
   category, its images never load anyway. This already covers the worst
   of the web at no CPU cost, and should run before anything expensive.
2. **A local classifier** on images above a size threshold (small images are
   icons and spacers). NudeNet-class ONNX models run on CPU in roughly
   50–200 ms for a 640px image. A news page with 30 photographs is then
   several seconds of CPU — noticeable, and worse on the low-end hardware a
   family machine often is.
3. **Caching by content hash** makes repeat views free and is what keeps
   this tolerable in practice.

The honest position: `nsfw` is achievable with a public model. `immodest`
is not a category any public model was trained for — modest dress is a
judgement about clothing coverage, not nudity — so it will need either a
custom-trained model or a person-detection proxy (any uncovered arms/legs
in a detected person), which will over-block beach photographs of children
and under-block a great deal else. That gap should be stated to families
rather than papered over.

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

So the build order is: `all` first (works now, no model), then source-based
suppression, then a classifier for `nsfw`/`suggestive` with blur, then
`immodest` — and inpainting last, as an experiment with a measured verdict
rather than a promise.


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
