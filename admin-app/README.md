# kosher-admin

GTK4/libadwaita settings app for KosherOS. A thin D-Bus client of kosherd —
all privileged logic lives in the daemon; polkit prompts come from the
desktop's own agent, and one `Unlock` opens a session so nothing prompts
again.

## What it shows

A **sidebar** (`sidebar.py`) down the left holds everywhere the app goes,
in two sections. **Family** first: a labelled "Add a person…" row (a bare
+ was not clear), then one row per account with its face, the preset it
is set up as and a badge when somebody is waiting on an answer, the guest
last. Then **Administration**: Activity, Protection, Apps, Updates. Every
row carries its own state on the right, so the sidebar answers "is
anything wrong?" before it is clicked. The app opens on the first person.
Every destination replaces the content pane rather than pushing onto it,
so nothing carries a back button the sidebar has already made
meaningless. Under 700sp it collapses to one pane at a time.

There is no overview board any more — the cards duplicated the sidebar and
the family said so. Two banners sit above the content pane on every page
and appear only when they have something to say: waiting requests (blue —
"2 requests waiting for you", one button opens them with their answers
inline) and filter health (amber; its Details goes to Protection).

**Administration** (`computer.py`) is three pages beside the feed, not the
row of tiles it once was. *Protection* opens on whether the filter is actually working —
the health rows are the page's first group, whatever they say, with Check
again beside them — then what applies to everyone and says so: ad and
tracker blocking (machine-wide because it is rendered into this computer's
own resolvers, so it cannot be answered per account), the three editable
word lists, and the guardian password with what it guards. *Apps* curates
what may be installed at all; *Updates* holds the version and the way back.

The **Activity** page (`feed.py`) is the filter's diary: what it blocked,
hid or refused, and what the admins changed, newest first. Who and when
are chosen from the header bar, so the feed keeps the full width and the
app has one sidebar rather than two. A blocked page has Allow on the row.

A person's **page** (`detail.py`) fills the content pane with the sidebar
beside it: an overview (the
protection as chips, drift with a Reset button, blocked today with Allow,
changes with who made them), then Filtering (the category grid on the
page, with preset / All / None), Pictures, YouTube, Apps and
Account.

`labels.py` holds every word for every daemon value; kosherd's tests read
it to make sure no setting is missing its label.

## Running the tests

    xvfb-run -a env PYTHONPATH=../kosherd/src:src python3 -m pytest tests -q

They build the real widgets against a stub client on a virtual display.
