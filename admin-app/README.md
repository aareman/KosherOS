# kosher-admin

GTK4/libadwaita settings app for KosherOS. A thin D-Bus client of kosherd —
all privileged logic lives in the daemon; polkit prompts come from the
desktop's own agent, and one `Unlock` opens a session so nothing prompts
again.

## What it shows

The home screen is the **family board** (`family.py`): one card per
account with the preset it is set up as (and how far it has drifted:
"Child, with 2 changes"), the same four lines of protection in the same
order — web, pictures, video, apps — and what the filter did for that
account today. The guest is a card like the others, dashed while off.
Below the cards, the computer-wide settings are tiles: Apps, Word lists,
Ads and trackers, Guardian password, Updates.

Two banners sit above the board and appear only when they have something
to say: waiting requests (blue — "2 requests waiting for you", one button
opens them with their answers inline) and filter health (amber, with the
details behind it; when all is well the board says "Filter running" with
the time it was checked).

The **Activity** tab (`feed.py`) is the filter's diary: what it blocked,
hid or refused, and what the admins changed, newest first, with the people
as filters down the side. A blocked page has Allow right on the row.

A card opens the person's **page** (`detail.py`): an overview (the
protection as chips, drift with a Reset button, blocked today with Allow,
changes with who made them), then Filtering (the category grid on the
page, with preset / All / None), Pictures & words, YouTube, Apps and
Account.

`labels.py` holds every word for every daemon value; kosherd's tests read
it to make sure no setting is missing its label.

## Running the tests

    xvfb-run -a env PYTHONPATH=../kosherd/src:src python3 -m pytest tests -q

They build the real widgets against a stub client on a virtual display.
