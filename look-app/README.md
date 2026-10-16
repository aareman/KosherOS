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

The glass: the blur behind the taskbar and the windows on or off, how
much, and how see-through the windows are, with a way back to the
KosherOS look. All of it is written into Blur My Shell's own settings
for this account. The rest of
the taskbar is tuned in its own settings, a right-click on the bar away.

Wallpaper, dark mode and text size stay in GNOME Settings, which does
them well; the app points there.

```sh
kosher-look
```
