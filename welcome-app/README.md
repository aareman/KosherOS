# Welcome

The window that opens the first time an account signs in, in place of
GNOME's "Welcome to GNOME" dialog. It says what this computer is and
where things are, and it says different things to an administrator and to
a managed user, because they have different things to find.

It starts from an autostart entry in every GNOME session and leaves at
once when the account has seen it (a stamp in `~/.config/kosher`). It
asks kosherd `GetMySettings`, the same call My Filter makes, so it knows
whether the person is an administrator, how their account is filtered and
how much time they have today. "Welcome" in the Apps menu opens it again.

```sh
kosher-welcome          # once, then never again for this account
kosher-welcome --again  # open it anyway
```
