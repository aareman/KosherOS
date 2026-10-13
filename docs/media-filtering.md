# Pictures and video

Text filtering is the easy half. What a family notices is imagery: an
allowed news site with an immodest photo, a shop's lingerie advertising, a
thumbnail on a permitted video page. This page describes how pictures and
video are judged, what is done with the ones that fail, and what that
costs on the machine the family owns.

## The levels

A picture level is set per account. Judging a picture means holding it,
which means reading the connection, so the levels act in *Filtered
internet* mode. In the other modes the level still decides one thing:
whether image search results are shown at all.

| Level | What is hidden |
|---|---|
| `none` | nothing |
| `nsfw` | explicit imagery |
| `suggestive` | explicit and suggestive |
| `immodest` | also immodest imagery; the default for a new account |
| `people` | also any picture with a person in it, whatever they wear |
| `all` | every remote image; pages render as text |

`all` is the only level that needs no judgement, so it is the only one that
is right every time. Every level below it depends on a detector and will
sometimes be wrong in both directions. `people` leans on the one thing the
detector is reliable about, whether there is a person in the picture, and
asks nothing about what they wear. Landscapes, products, diagrams and text
keep showing at `people`; photographs of anyone, in anything, do not.

## How a picture is judged

**At `all`** the proxy replaces any image response with a placeholder.
Cheap, instant and reliable, and many sites stay readable.

**At the other levels** each stage runs only because the one before it
could not settle the question:

1. **Size.** Under 2.5 KB an image is an icon, a spacer or a tracking
   pixel, and it never reaches the detector.
2. **Source.** If the page's domain is in a category this account blocks,
   its imagery goes with it. No detector, and no chance of one being wrong.
3. **Cache.** A verdict is kept for a month, keyed by the hash of the
   image bytes and the model version, on disk and shared by every account
   on the machine. The same logo and avatar come back on every page of a
   site, so most views cost nothing.
4. **The detector.** A nudity model of about 5 MB runs on the CPU in a
   small thread pool with a two-second deadline. The proxy's response hook
   awaits the worker, so the rest of the machine's traffic carries on while
   a picture is judged, and a worker that outlives the deadline still
   caches its verdict for next time.

The detector returns labelled regions rather than one score, which is what
makes the ladder meaningful: exposure maps to `nsfw`, covered but
prominent to `suggestive`, weaker signals to `immodest`. A face on its own
is never a finding; otherwise every photograph of a person would trip the
filter.

**When it cannot look, it hides.** No model installed, a timeout, a
corrupt image: the picture is withheld, never shown. The null detector
returns nothing rather than "clean", so that "looked and found nothing"
can never be confused with "could not look".

### The immodest level

The detector has no label for a bare arm or a bare leg, so `immodest`
needs more than the detector alone, and three signals are combined.

**What the page says.** A picture is matched to its page through the
`Referer` header against a cache of recent page verdicts. If the page
scored at or beyond the account's tolerance and the picture contains a
person, the picture is hidden. Neither half would do on its own: hiding
every picture on a page that scored immodest takes out the shop's own
logo, and hiding every picture with a person takes out a news photograph
on a page about nothing in particular.

**Skin over a figure.** The body is estimated from a detected face, or
from the detected parts when there is no face, and the fraction of
skin-toned pixels inside that box promotes a "clean" picture to immodest
past a limit. Legs are measured on their own as well, in the lower middle
of the body box, because a covered top dilutes bare legs below the
whole-figure line. The measurement is only ever taken inside a detected
figure and never used to find one, because it sees some skin tones better
than others and reads khaki as skin. The figure is measured whether the
model calls the face male or female, and a picture that measures clean
below a face is measured again over the person detector's own box, because
the box guessed from a face is upright and a figure bent or thrown would
otherwise measure as background. That is what catches classical statues
and old paintings of nudes, at the price that marble sometimes reads as
skin.

**A person detector.** A skirt photographed from the hips down has no face
and no labelled part. So when the nudity model found no face and the
picture has enough skin-toned area to matter, a small person detector is
asked whether there is anyone there, and its box is what the skin
measurement runs on. On a shop's grid for that search it recovered ten of
the eleven pictures that had been passing.

A family that wants tzniut enforced strictly should still choose `people`
or `all`, which need no judgement of what someone is wearing.

### Covering the region

A picture that fails is not always hidden whole. When the detector's
regions cover a small part of the picture, those regions are frosted and
the rest of the picture is kept, so the page keeps its layout and the site
stays usable. The region is shrunk to a handful of cells, smoothed back
up, pulled toward its own average colour, given light noise so the frost
cannot be undone by deconvolution, and pasted through a feathered edge.
The shape is gone in the first step; the rest is making the cover sit
quietly in the picture. Regions are grown by 60% because the detector's
boxes are tight, and when the covered area would be most of the picture
the picture is hidden whole instead.

Each format comes back as itself, so a WebP does not turn into a PNG five
times the size. The covered pixels are remembered for a while, so a reload
or the same photo on the next page is not frosted again. If the picture
cannot be edited at all, it is hidden rather than passed through.

### Pictures that move

A GIF, an animated PNG or an animated WebP is a short clip and is treated
as one: four frames spread through the animation are judged and the
strongest frame's verdict is the picture's. Anything the detector's image
reader cannot open, GIF and AVIF included, is handed to it as a JPEG. An
animation that must be covered is hidden whole, because a cover on one
frame means nothing on the others. WebM is video and takes the video path.

## Video

Video is judged by its source first, then by a few of its frames.

The cheap levers come first: the source, the page it is on, the poster
thumbnail, and for YouTube the per-category and approved-channel limits,
which are precise because YouTube labels its own videos.

Then, for an account whose pictures are filtered, the clip itself. Four
keyframes spread through the clip, decoded small, go through the same
detector and the same ladder as a photograph, and the strongest frame is
the clip's level. The verdict is cached by address and size for a month,
so the cost, a decode plus four detections, is paid once per clip. The
decoder ships with the H.264, VP9 and AV1 codecs Fedora's own ffmpeg
leaves out.

The rules are decided from the headers before a byte of the body is
fetched:

| Situation | `nsfw` | modesty levels | `all` |
|---|---|---|---|
| source in a blocked category | refused | refused | refused |
| verdict already cached | as it says | as it says | refused |
| clip, or a piece of one, small enough to hold (40 MB) | held, sampled, then passed or refused | same | refused |
| a header or audio-only segment, nothing to judge | passed | passed | refused |
| too big to hold | passed | refused | refused |
| no decoder on this machine | passed | refused | refused |
| could not be read, or missed the 15 s deadline | passed, marked unchecked | refused | refused |

A clip that arrives as `application/octet-stream`, which CDNs do, is
recognised by its address's extension and treated as video. A range from
the middle of a clip is held and decoded like any other piece, because a
fragment of a DASH stream or a WebM cluster stands on its own.

Two things to know. Large files are not held: long-form video on the web
is almost always segmented, each segment is small and judged on its own,
so a stream is checked piece by piece, while a single progressive file
over the cap keeps the behaviour in the table. And frames are sampled, not
watched: four frames from a two-minute clip can miss a second of anything.
This judges what a video is about. The guarantee is still `all`, and the
mildest level still lets through what it cannot check, because a missing
decoder must not take video away from an account that never asked for it
to be checked.

A refused clip is a `403` with an `x-kosheros: video-blocked` header, sent
in place of the headers so the body is never downloaded. A passed one
carries `x-kosheros: video-checked`.

## What is held and what streams

Only what the filter reads is held: pages, pictures for an account that
filters them, the JSON of a few known sites, and clips small enough to
sample. Downloads, archives, fonts, audio, anything large that is none of
those, and a connection that belongs to no filtered account go straight
through as they arrive.

## Shopping sites

Blocking women's clothing on a large shop is a text problem, not a vision
problem. The department is in the address and the page: a search carries a
department parameter, a product page carries breadcrumbs, a category page
carries a node id. Reading that costs microseconds, is exact, and never
mistakes a sofa for a person. So department rules come first, and vision is
for imagery on pages that carry no such labels.

**Department rules** describe a part of a site the family uses, and they
fire for accounts that block the `immodest` category, the same setting
that blocks lingerie retailers outright. They are matched on the request,
before a byte of the page is fetched, and in search results too, so a
result cannot lead somewhere a click would be blocked. The block page's
reason names the term and the host, "the fashion-womens department of
amazon.com", because an admin has to be able to disagree with a specific
word rather than with a probability.

**The shop's own search box and autocomplete** are filtered too, or the
department rule is worth nothing. A search typed into the shop's box is
matched against the same department vocabulary plus the blocked search
terms; `laptop stand` and `sefer torah` go through untouched. Autocomplete
comes back as JSON that every site nests differently, so rather than learn
each schema the filter walks whatever came back and drops list entries
whose text is blocked, leaving the structure alone.

**Removing the item beats judging the page.** A shop names its whole
catalogue in the navigation of every page, and scoring that gives two bad
answers: judge it strictly and the shop is blocked outright, judge it
loosely and the sidebar stays on screen. So on a covered site the
offending links, list items and options are removed first, and what is
left is judged normally. The document is walked with the standard
library's HTML parser and everything else is left byte-identical, because
a filter that rewrites markup it does not understand breaks sites in ways
nobody can debug. On any parse trouble the original page is returned
untouched. A plain substring scan runs first and skips the parse unless
one of the words is present, which takes a clean page from tens of
milliseconds to two. Pages above 2 MB are not rewritten, and when nothing
was removed the scorer's floor for that page drops to `suggestive`, so a
page about socks is not convicted by a sidebar nobody could take out.

Three rules keep this from becoming an outage. A site with its own rules
is not also judged by the generic list, so tuning one shop cannot widen
another. The generic list fires only on shop-shaped addresses, because an
article whose title contains the word is not a department. And the
vocabulary is checked against what people need: `hosiery` is not a term
because "Socks & Hosiery" is where you buy socks, and the test suite
asserts the sock department survives. A site can override which tags and
terms are stripped in its rules, so a layout the defaults do not reach is
a rules-file edit rather than a release.

## Image search

In filtered mode a search thumbnail loads from its origin through the
proxy and is judged like any other picture, so image search works. In the
modes that never read the connection, thumbnails would arrive unexamined,
so an account that filters pictures does not get them there.

## Running on the family's own computer

Filtering happens on the device. No image and no browsing history leaves
the house, the filter works without a subscription, and there is no
service to breach. The cost is that the accuracy ceiling is whatever a
CPU-only laptop can do, often an old one, so every layer is designed for
that machine.

`just benchmark CPUS=2` measures the filter inside the built image on two
cores, which is the machine this product is for:

| Operation | Per call |
|---|---|
| category lookup | 0.004 ms |
| a shop rule on an address | under 0.01 ms |
| element strip, page has none of the words | 0.4 ms |
| content score, 28 KB page | 6.7 ms |
| bad-language scan, 28 KB page | 7.3 ms |
| image verdict, cache hit | 0.2 ms |
| **detection, 800×600** | **440 ms** |
| resident memory, everything loaded | 150 MB |

Detection on two cores is 200 to 600 ms per image, and thread tuning does
not move it. At that rate a news page with thirty photographs is fifteen
seconds of waiting for a half-checked page. So the proxy keeps a rolling
median of its own detection time, and above 400 ms it stops trying and
hides pictures instead: instant, never wrong, and honest about what the
computer can do. Cached verdicts are still served. The window rolls, so a
machine that was briefly busy recovers on its own.

And it says so. The proxy publishes whether it is checking, too slow, or
has no model, and the admin app shows it as an amber count beside
Protection and spells it out on the Protection page. A version of this
that said nothing would teach people that blank pictures mean the computer
is broken, which is how a filter ends up switched off.

The text path is not where the time goes. Judging a page costs about 20 ms
of Python across scoring, bad language and element stripping, under 5% of
one image. The expensive parts are already not Python: detection is ONNX
Runtime, the category database is SQLite, image decoding and covering are
Pillow. The wins are in the model, in not running it, and in algorithms,
not in another language. The one measurement not yet taken is the proxy's
throughput under a browser's concurrent connections.

Deliberately no Redis on the device: another daemon holding RAM on a
machine already short of it, to cache something SQLite handles.

## What is not built

**A tzniut classifier.** The `immodest` level is weaker than the two above
it, and the way to strengthen it is not to train "is this immodest"
directly: that needs tens of thousands of labelled examples, and
assembling such a corpus is work a frum organisation cannot ask anyone to
do. Two paths avoid it. Off-the-shelf pose and clothing-segmentation
models, trained on ordinary photographs, give joint positions and which
pixels are skin versus fabric, and halachic criteria become readable rules
over that, which a rav can review and which can differ per family without
retraining. Or a small classifier fine-tuned on academic fashion datasets
that already label sleeve length, neckline and hem length. Either runs in
tens of milliseconds. Shopping rules first, then pose geometry, then a
fashion-attribute classifier, is the order that gets the most filtering
per unit of effort.

**Inpainting people out of pictures.** Possible, with segmentation plus an
inpainting model, and not viable inline: one to three seconds per image on
a CPU, and half a gigabyte of model weights. Frosting the region is what
shipped. Inpainting stays an experiment for capable hardware.

Every approach judges images on the family's own machine. Sending them to
a service would be faster and better, and is rejected for the same reason
it is tempting.
