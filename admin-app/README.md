# kosher-admin

GTK4/libadwaita settings app for KosherOS. A thin D-Bus client of kosherd —
all privileged logic lives in the daemon; polkit prompts come from the
desktop's own agent, and one `Unlock` opens a session so nothing prompts
again.

## What it shows

A **sidebar** (`sidebar.py`) down the left holds everywhere the app goes,
in two sections. *The family* is per person: the board and the activity
feed. *This computer* is the rest: Protection, Apps, Updates. Each row
carries its own state on the right — how many people, how many apps,
whether the filter is running — so the sidebar answers "is anything
wrong?" before it is clicked. Under 700sp it collapses to one pane at a
time.

The **family board** (`family.py`) is the home screen: one card per
account with the preset it is set up as (and how far it has drifted:
"Child, with 2 changes"), the same four lines of protection in the same
order — web, pictures, video, apps — and what the filter did for that
account today. The guest is a card like the others, dashed while off.

Two banners sit above the board and appear only when they have something
to say: waiting requests (blue — "2 requests waiting for you", one button
opens them with their answers inline) and filter health (amber; its
Details goes to Protection).

**This computer** (`computer.py`) is three pages, not the row of tiles it
used to be. *Protection* opens on whether the filter is actually working —
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

A card opens the person's **page** (`detail.py`), pushed inside the
content pane so the sidebar stays put: an overview (the
protection as chips, drift with a Reset button, blocked today with Allow,
changes with who made them), then Filtering (the category grid on the
page, with preset / All / None), Pictures, YouTube, Apps and
Account.

`labels.py` holds every word for every daemon value; kosherd's tests read
it to make sure no setting is missing its label.

## Running the tests

    xvfb-run -a env PYTHONPATH=../kosherd/src:src python3 -m pytest tests -q

They build the real widgets against a stub client on a virtual display.
