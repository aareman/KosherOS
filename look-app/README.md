# Look

Your own desktop, chosen and tuned by you. Open to every account, no
password.

Two things live here:

- **Which desktop you use.** Classic (a taskbar and an Apps button),
  Tiling (PaperWM's scrolling row), or Advanced (a separate niri
  session). The choice is a preference, not a protection: the filter is
  per account at the network layer and does not care which program draws
  the windows, so the person picks for themselves. The app calls
  `SetMyLayout` on `org.kosherlinux.Daemon1.Profiles`, gated by
  `org.kosherlinux.set-own-desktop`, which every active local user holds
  with no prompt; the daemon takes the account from the D-Bus connection,
  so nobody can choose for anybody else. Classic and Tiling are applied to
  the running session straight away (`kosher-layout --layout`); Advanced
  is a different session and waits for the next sign-in.
- **How the classic taskbar looks.** How see-through it is, whether the
  wallpaper is blurred behind it, and its size, written straight into the
  extensions' own settings for this account, with a way back to the
  KosherOS default. The sign-in helper leaves these alone (it writes a
  plan only when the layout changes), so they stick.

Wallpaper, dark mode and text size stay in GNOME Settings, which does
them well; the app points there.

```sh
kosher-look
```
