# KosherOS Store

Browse apps available to your account and ask kosherd to install, update,
or remove them. Installation progress appears on app cards and details pages.

Click an app card (or focus it and press Enter) to see its description,
screenshots, license, version, and website when available. The action button
on the card still installs, updates, or removes the app directly. Back returns
to the same search and category.

Details are fetched only when an app is opened, and successful lookups are
cached for the window's lifetime. Flatpak details come from that app's Flathub
API endpoint; screenshots are downloaded individually as you view them. Nix
package details come from a read-only `nix eval` of the selected package,
including its description, license, maintainers, and main command. No packages
are built or installed to show details. Network requests and Nix evaluation run
as the current user, off the UI thread. Missing metadata or screenshots can be
retried without losing the app's install controls.

Run `just store-demo` to try the interface with sample installation state.
App detail lookups in the demo use the same upstream sources as the real Store.
