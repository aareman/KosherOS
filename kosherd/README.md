# kosherd

The Kosher Linux privileged management daemon, policy engine, and CLI.

- `kosherd` — root daemon on the system bus (`org.kosherlinux.Daemon1`),
  polkit-gated. The only writer of enforcement state.
- `kosherctl` — CLI client, plus offline `validate` / `render-nft` /
  `render-dnsmasq` commands for fast development.
- Pure modules (`policy`, `nft`, `dns`, `guardian`) have no GObject
  dependency and are fully unit-tested: `python -m pytest tests/`.
