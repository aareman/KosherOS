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
