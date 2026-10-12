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
| `immodest` | also immodest imagery; the frum default |
| `people` | also any picture with a person in it, whatever they wear |
| `all` | every remote image; pages render as text |

`all` is the only level that needs no judgement, so it is the only one that
is exactly right every time. Everything below it depends on a classifier
and will be wrong sometimes in both directions. `people` leans on the one
thing the models are reliable about, whether there is a person in the
picture, and asks nothing about what they wear: tight or sheer clothing on
a covered figure ("comic book super women") has no skin to measure and no
label to fire, so `immodest` will keep missing it, and a level that
pretended otherwise would be wrong quietly. Landscapes, products, diagrams
and text keep showing at `people`; photographs of anyone, in anything, do
not.

## How a picture is judged

**`all`**: the proxy replaces any image response with a placeholder. Cheap,
instant, completely reliable. Also genuinely usable: many sites remain
readable, and a person who needs a picture can ask for the page.

**`nsfw` / `suggestive` / `immodest` / `people`** (`kosherd/vision.py`):
each stage runs only because the one before it could not settle the
question.

1. **The page's domain (free).** If it is in a category this account
   blocks, its imagery goes with it. No model, and no chance of a model
   being wrong.
2. **Department rules on the address (free).** The shopping case, below.
3. **Size threshold (free).** Under 2.5 KB an image is an icon, a spacer or
   a tracking pixel. It never reaches the model.
4. **Cache by content hash (~1 ms).** Keyed by the SHA-256 of the image
   bytes plus the model version, stored on disk in SQLite and shared by
   every user on the machine, with a verdict kept for a month. The same
   logo, banner and avatar come back on every page of a site, so repeat
   views cost nothing, which is most views. A weak machine benefits from
   this more than a fast one, so the cache is sized generously. Clip
   verdicts live in the same file, keyed by address and size. Deliberately
   no Redis on the device: another daemon holding RAM on a machine already
   short of it, to cache something SQLite handles.
5. **The detector (tens to hundreds of ms).** NudeNet's ONNX model, about
   5 MB, on the CPU, in a small thread pool with a 2 s deadline, awaited
   rather than waited for: the proxy runs its hooks on one asyncio event
   loop, so the picture being judged waits on a worker while the rest of
   the machine's traffic carries on, and a worker that outlives the
   deadline still caches its verdict for the next time.

The detector returns **labelled regions**, not one score, which is what
makes the ladder meaningful: exposure maps to `nsfw`, covered-but-prominent
to `suggestive`, weaker signals to `immodest`. A detected face on its own
is never a finding; otherwise every photograph of a person trips the
filter, which is an off switch with extra steps rather than a filter.

**When it cannot look, it hides.** No model installed, inference timed out,
a corrupt image: the answer is to hide the picture, never to show it. The
null detector returns *nothing* rather than "clean" specifically so that
"looked and found nothing" cannot be confused with "could not look".

### The immodest level

`immodest` is genuinely weaker than the two levels above it. The detector
has no label for a bare arm or a bare leg, so a clothed model in a
lingerie catalogue comes back clean from the labels alone. Three further
signals close most of that gap.

**What the page says.** A picture is hidden when the page it came from
scored at or beyond this account's tolerance **and** the picture contains a
person. Neither half would do alone: hiding every picture on a page that
scored immodest takes out the shop's own logo and navigation icons; hiding
every picture containing a person takes out a news photograph on a page
about nothing in particular. The picture is matched to its page through the
`Referer` header, against a bounded cache of recent page verdicts, since a
browser fetches a page's images within seconds of the page.

**Skin over a figure.** Beachwear, bare shoulders and arms, legs under a
short skirt: none of these has a label. So skin is measured, never over a
whole picture, only over a **figure**. The whole body is estimated from a
detected face (or from the union of detected parts when there is no face),
and the fraction of skin-toned pixels in that box, by the classic YCbCr
gate, promotes a "clean" picture to immodest past `SKIN_LIMIT`. Legs are
measured on their own as well (`LEGS_SKIN_LIMIT`, the lower middle of the
body box), because a covered top dilutes bare legs below the whole-figure
line. The rule runs below any face, male or female: the model's sexing of a
face is a coin toss, and a bare male chest is immodest by label already.

**A person detector for the figure with no face.** A skirt photographed
from the hips down has no face and no labelled part. A skin rule with no
figure to anchor it was measured and rejected: against a hundred and fifty
beige products, any threshold that caught most of the legs hid three
quarters of the furniture. So YOLOX-Nano (`kosherd/persons.py`,
Apache-2.0, 3.5 MB, about forty milliseconds) answers "is there a person
here", asked only when the nudity model found no face and the picture has
enough skin-toned area to matter, and its tight box is what the skin
measurement then runs on. A face is left out of that measurement: in a
head-and-shoulders portrait the face is a third of the box and would hide
every clothed portrait on a news page. A picture that a face-derived box
measured clean is measured again over the detector's own box, because a
figure bent, crouching or thrown has its body beside its face. And a guess
below the detector's 0.5 line is believed when the region it names is bare
enough, on a sliding scale (`WEAK_PERSON_CONFIDENCE`, `BARE_SKIN_LIMIT`):
a guess of 0.2 needs a region that is 45 percent skin, falling to the
ordinary line as the score approaches 0.5. That is what catches classical
statues and old paintings, on which a model trained on photographs hedges
and whose marble reads as skin under the colour gate.

Measured on 24 statues, 16 nude paintings and 47 controls (animals, food,
toys, furniture, clothed people, clothed statues and clothed portraits):

| set | full size | 300 px thumbnail |
|---|---|---|
| statues hidden | 23 of 24 | 22 of 24 |
| nude paintings hidden | 16 of 16 | 16 of 16 |
| controls wrongly hidden | 14 of 47 | 13 of 47 |

The limits are known and stated rather than hidden. The colour gate sees
some skin tones better than others, so it is stricter with some families
than others; that is why it is only ever applied inside a detected figure,
never used to *find* one. Khaki reads as skin to it: of twenty-five
photographs of men modelling khaki trousers, six are hidden. Clothed marble
statues are often covered, a price accepted for the statues the rule was
made for. A dark bronze has no skin tone to measure and nothing else to go
on. A family that wants tzniut enforced strictly should still choose
**hide all pictures** or **people**, which need no judgement of clothing.

### Covering the figure, not the page

A picture the ladder convicts is not blanked. The offending regions are
**frosted** (`kosherd/imageedit.py`): each is shrunk to a handful of cells,
smoothed back up, pulled a third of the way toward its own average colour,
given light noise (so it cannot be undone by deconvolution) and pasted
through a feathered mask. The shape is gone in the first step; the rest is
making it sit quietly in the picture. Family testing rejected the
alternatives in turn: tight pixel blocks left a fringe, blocks plus static
shouted about it, and a plain blur at any radius that keeps a page fast
left a figure's silhouette and skin tone readable. Regions are grown by
60% (the detector's boxes are tight), and when the covered area would be
most of the picture the picture is hidden whole instead. Each format comes
back as itself (a covered WebP is not returned as a five-times-larger
PNG), and the covered pixels are remembered in a small in-memory cache so
a reload is not decoded, frosted and re-encoded again. If the picture
cannot be edited (an unreadable format, no Pillow) the whole image is
hidden rather than passed through.

Inpainting the figure out (person segmentation plus LaMa) is technically
possible and not viable inline: one to three seconds per image on a CPU,
and about 500 MB of model weights in the image. It stays an experiment for
capable hardware, not a promise in the default path.

### Pictures that move

A GIF, an animated PNG or an animated WebP is a short clip, and is treated
as one: `vision.animation_frames` takes four frames spread through the
animation and the strongest frame's verdict is the picture's. Anything the
detector's image reader cannot open (GIF, AVIF) is handed to it as a JPEG
(`vision.prepare`), so a still GIF is judged like any other picture. An
animation that must be covered is hidden whole: a cover placed on one
frame means nothing on the others. WebM is video and takes the video path.

## Video

Judged by its source first, then by a few of its frames.

The cheap levers still come first: the source (a blocked category), the
page (scored by its words), the poster thumbnail (an image, filtered like
any other), and for YouTube the per-category and approved-channel limits
described under [content filtering](content-filtering.md#youtube), which
are precise because YouTube labels its own videos.

Then, for an account whose pictures are filtered, the clip itself
(`kosherd/videocheck.py`). A video is pictures at thirty a second, and
decoding all of them is out of the question on two cores, but it never
needed all of them. Four keyframes spread through the clip, decoded small
(480 px on the short side, which is what the detector wants anyway), go
through the same detector and the same ladder as a photograph; the
strongest frame is the clip's level. The verdict is cached by address and
size for a month, so the cost (a decode plus four detections, one to two
seconds on two cores) is paid once per clip, not once per view. Decoding
is PyAV (FFmpeg's libraries as a wheel, with the H.264, VP9 and AV1
decoders Fedora's own `ffmpeg-free` leaves out).

The rules, in order, decided **from the headers before a byte of the body
is fetched** (`responseheaders`):

| situation | mildest level (`nsfw`) | modesty levels | `all` |
|---|---|---|---|
| source in a blocked category | refused | refused | refused |
| verdict already cached | as it says | as it says | refused |
| clip or piece of one small enough to hold (≤ 40 MB) | held, sampled, then passed or refused | same | refused |
| a header or audio-only segment (nothing to judge) | passed | passed | refused |
| too big to hold | passed | refused | refused |
| no decoder on this machine | passed | refused | refused |
| could not be read (a slice of a plain file, a codec this build lacks), or missed the 15 s deadline | passed, marked unchecked | refused | refused |

A clip that arrives as `application/octet-stream` (CDNs do this) is
recognised by its address's extension (`.mp4`, `.webm`, `.m4s`, …) and
treated as video all the same; a `.gif` served that way is a picture. A
range from the middle of a clip is held and decoded like any other piece: a
fragment of a DASH stream or a WebM cluster stands on its own.

Two things to know. **Large files are not held.** Long-form video on the
web is almost always segmented (HLS/DASH); the manifest is text that points
at segments, and each segment is small and judged on its own, so a stream
is checked piece by piece. A single progressive file over the cap cannot be
sampled without holding it, so it passes at the mildest level and is
refused at the modesty ones. **Frames are sampled, not watched.** Four
frames from a two-minute clip can miss a second of anything; this judges
what a video is about. The guarantee is still `all`, and the mildest level
lets through what it cannot check, because a missing decoder must not take
video away from an account that never asked for it to be checked.

A refused clip is a `403` with `x-kosheros: video-blocked`, sent in place
of the headers so the body is never downloaded; a passed one carries
`x-kosheros: video-checked=<level>`.

## Shopping sites: rules before pictures

Blocking women's clothing on Amazon is a **metadata** problem, not a vision
one. The department is in the URL and the page: a search carries an
`i=fashion-womens` style department parameter, a product page carries
breadcrumbs and a department field, a category page carries a node id.
Reading that costs microseconds, is exact, and never mistakes a sofa for a
person. Running a vision model over 40 product thumbnails to discover what
the page already says in text would be slower, less accurate, and
pointless. The same applies to most large sites worth worrying about: they
are databases with categories, and the categories are in the markup.

So `kosherd/siterules.py` and `/usr/share/kosher/site-rules.json` describe
a *part* of a site the family uses (Amazon is not a site a family blocks, a
department on it is), and they fire only for accounts that block the
`immodest` category, the same setting that blocks lingerie retailers
outright. They are matched on the request, before a byte of the page is
fetched, and in search results too, so a result cannot lead somewhere a
click would be blocked. The reason names the term and the host ("the
fashion-womens department of amazon.com"), because an admin has to be able
to disagree with a specific word rather than with a probability.

**The site's own search box and autocomplete.** Blocking the department is
worth nothing if the search box on the same page reaches it anyway, and
autocomplete is worse than the results: it puts the words on screen
unprompted, while somebody is typing something else. A search typed into a
shop's own box is matched against the same department vocabulary the
address rules use (`?k=lingerie` on Amazon, `?_nkw=bikini` on eBay) plus
the explicit search blocklist; `?k=laptop stand` and `?_nkw=sefer torah`
go through untouched. Autocomplete comes back as JSON nested differently on
every site, so `kosherd/suggest.py` walks whatever came back and drops
**list entries** whose text is blocked, leaving the structure alone: the
one edit that cannot break a response's shape. A suggestion is a few words
rather than a page, so the term list answers there and not the content
scorer, which needs more evidence than one word can carry.

**Removing the item beats judging the page.** A shop names its whole
catalogue in the navigation of every page. Scoring that gives two bad
answers and no good one: judge it strictly and Amazon is blocked outright,
judge it loosely and the sidebar stays on screen. So on a covered site the
offending items are removed first, and what is left is judged normally.
`kosherd/elementfilter.py` walks the document with the standard library's
`html.parser`, drops the `<a>`, `<li>` or `<option>` elements whose text
or address matches the site's department vocabulary, and leaves everything
else **byte-identical**: entities, comments, script bodies, attribute
quoting and all. A filter that rewrites markup it does not understand
breaks sites in ways nobody can debug, so on any parse trouble the
original page is returned untouched. A site can override both halves in
its rules (`strip_tags` and `strip_terms`), so a layout the defaults do not
reach is a rules-file edit rather than a release. A plain substring scan
runs first and skips the parse entirely unless one of the words is
present, which takes a 0.6 MB clean page from 83 ms to 2 ms; pages above
2 MB are not rewritten at all. When a page is too large or fails to parse,
the catalogue vocabulary is still in the text and the scorer's floor drops
to *suggestive* for that page, so a page about socks is not convicted by a
sidebar nobody could take out.

**Three rules keep this from becoming an outage.** A site with its own
rules is *not* also judged by the generic list, so tuning one shop cannot
silently widen another. The generic list only fires on shop-shaped
addresses (`/shop/`, `/collections/`, `/category/`), because an article
whose title contains the word is not a department. And the vocabulary is
checked against what people actually need: "Socks & Hosiery" is where you
buy socks, so `hosiery` is not a term (the tights department says `tights`
or `stockings`, and those are), and the test suite asserts the sock
department survives.

Image search results on the KosherOS search page are thumbnails loaded from
their origin through the proxy, and are judged like any other picture; see
[search](search.md#images).

## Running on the family's own computer, including a slow one

Filtering happens on the device. No image and no browsing history leaves
the house, the filter keeps working without a subscription, and there is no
service to breach. The cost of that choice is that the accuracy ceiling is
whatever a CPU-only laptop can do, often an old one, so every layer has to
be designed for that machine rather than a developer's. Sending images to
a service would be faster and better, and is rejected for exactly the
reason it is tempting.

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

Detection is 200–600 ms per image on two cores, and no amount of thread
tuning moves it. At that rate a news page with thirty photographs is
fifteen seconds of waiting for a *half-checked* page, since many would hit
the timeout and be hidden anyway. That is worse for the person than the
setting that needs no model at all.

So the machine is measured instead of assumed. `ImageFilter` keeps a
rolling median of its own detection time, and above 400 ms it stops trying
and hides pictures instead: instant, never wrong, and honest about what
the computer can do. Verdicts already in the cache are still served, since
they cost nothing and are just as accurate on a slow machine. The window
rolls, so a machine that was briefly busy recovers on its own.

**And it says so.** The proxy publishes its state, `checking`, `too_slow`
or `no_model`, and kosherd reports it through `FilterStatus` to the admin
app's Protection page and to `kosherctl status`, as [content
filtering](content-filtering.md#saying-when-the-filter-is-not-filtering)
describes. A filter that has quietly stopped doing something is worse than
one that never did it, because the family is relying on it; and the
version of this feature that says nothing teaches people that blank
pictures mean the computer is broken, which is how a filter ends up
switched off.

### Would another language be faster? Mostly no

Judging a page costs about 20 ms of Python across scoring, profanity and
element stripping. Judging one picture costs 440 ms. So the entire Python
text path is under 5% of the cost of a *single* image, and a page with a
dozen pictures makes it a rounding error. The expensive parts are already
not Python: detection is ONNX Runtime (C++), the category database is
SQLite (C), image decoding and covering are Pillow (C). Rewriting the parts
that *are* Python in Rust or Go might recover 15 ms per page and would cost
the property that makes this project maintainable by one person with
occasional contributors.

Where the wins actually are, in order: the model (input resolution, int8
quantisation, a smaller or better architecture are the only things that
move the 440 ms); not running it (the staged design, the content-hash
cache and the `too_slow` fallback); and algorithms inside Python (the
profanity scan went from 40 ms to 4 ms by removing named capture groups, a
ten-fold win from measuring, not from a rewrite). The one honest candidate
for another language is proxy throughput under concurrency, which is a
different measurement from the ones in the table and has not been taken
yet; and the other reason to want a compiled component, a single static
binary for the [standalone package](standalone.md), is a distribution
argument rather than a performance one.

### What is held and what streams

Only what the filter reads is held: pages, pictures for an account that
filters them, the JSON of a few known sites, and clips small enough to
sample. Downloads, archives, fonts, audio, anything large that is none of
those, and a connection that belongs to no filtered account go straight
through as they arrive.

## A stronger tzniut classifier

What the `immodest` level still misses (covered but tight or sheer
clothing, hemlines and sleeves judged as such) needs a model that reports
what is worn, and the one approach that cannot be taken is to train "is
this immodest" directly: a supervised classifier needs tens of thousands
of labelled examples, and assembling and labelling a corpus of immodest
imagery is exactly the work a frum organisation cannot ask anyone to do.

Two paths avoid that entirely:

1. **Geometry over pose and segmentation.** Off-the-shelf person-keypoint
   and clothing-segmentation models, trained on ordinary photographs, give
   joint positions and which pixels are skin versus fabric. Halachic
   criteria are then expressed as *rules* over that: are the elbows
   covered, the knees, the collarbone. The rules are readable, a rav can
   review them, and they can differ per family without retraining
   anything. "The model decided" is not an answer a posek can work with.
2. **Fashion attribute datasets.** DeepFashion and Fashionpedia are
   academic datasets labelled with sleeve length, neckline, hem length and
   so on, the exact vocabulary tzniut cares about, already annotated, with
   no objectionable content to handle. Fine-tuning a small classifier on
   those attributes gets a model that reports "sleeveless, above-knee"
   rather than a single opaque score.

Either runs in the tens of milliseconds on a CPU at 224 px, and behind the
same cache and stages as today's detector. The order that gets the most
filtering per unit of effort: pose geometry driving the cover, then a
fashion-attribute classifier to sharpen it. A bespoke end-to-end model is
the last thing to reach for, not the first.
