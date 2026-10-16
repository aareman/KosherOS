# Look

Your own desktop, chosen by you. Open to every account, no
password.

Here a person picks which desktop they use: Classic (a taskbar and an
Apps button), Tiling (PaperWM's scrolling row), or Advanced (a separate
niri session). The choice is a preference, not a protection: the filter is
per account at the network layer and does not care which program draws
the windows, so the person picks for themselves. The app calls
`SetMyLayout` on `org.kosherlinux.Daemon1.Profiles`, gated by
`org.kosherlinux.set-own-desktop`, which every active local user holds
with no prompt; the daemon takes the account from the D-Bus connection,
so nobody can choose for anybody else. Classic and Tiling are applied to
the running session straight away (`kosher-layout --layout`); Advanced
is a different session and waits for the next sign-in.

The taskbar is not tuned here. It has its own settings, a right-click
on the bar away.

Wallpaper, dark mode and text size stay in GNOME Settings, which does
them well; the app points there.

```sh
kosher-look
```
