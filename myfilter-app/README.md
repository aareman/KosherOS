# My Filter

A read-only window showing what applies to **your own** account: the
filter mode, in plain language, and the handful of settings that follow
from it.

Open to every user, needs no password, and changes nothing — there is no
control in it that writes. It exists because a filtered account should
not be a mystery to the person using it. Someone who can see the rules is
far likelier to accept them than someone who only ever meets a blocked
page, and a parent can sit with a child and look at this together.

Reads `GetMySettings` on `org.kosherlinux.Daemon1.Profiles`, which is
gated by `org.kosherlinux.read-own-settings` — held by every active local
user with no prompt. The daemon takes the account from the D-Bus
connection rather than from an argument, so the window can only ever show
the person in front of it.

What it deliberately does **not** show is the filter's current health.
`FilterStatus` stays admin-only, because "picture checking has backed off
right now" tells someone when the machine is at its weakest. That belongs
in KosherOS Admin, where it already is.

```sh
kosher-my-filter
```
