# KosherOS portal

Self-hostable, single-tenant remote configuration for enrolled devices.
Devices poll it for **signed** policy documents; it never connects *to* a
device, so no family machine needs an inbound port.

## Trust model

The portal holds an Ed25519 private key (generated on first start, stored
in its SQLite database) and signs every policy document. A device pins the
public half when it enrols, then accepts a policy only if the signature
matches **and** the revision is higher than the one it already applied —
so a policy cannot be forged, tampered with in transit, or replayed.

Losing the portal does not unlock a device: the last applied policy keeps
being enforced locally, and unenrolling leaves it in place.

## Running it

```sh
podman build -t kosher-portal -f portal/Containerfile portal
podman run -p 8000:8000 -v kosher-portal:/var/lib/kosher-portal \
    -e KOSHER_PORTAL_ADMIN_TOKEN="$(openssl rand -hex 32)" kosher-portal
```

`KOSHER_PORTAL_ADMIN_TOKEN` guards the admin API; without it the admin
endpoints refuse everything rather than falling back to a default.

## Enrolling a device

```sh
# on the portal: create the device and get a one-time code
curl -X PUT ... /api/v1/admin/devices  -d '{"name": "Family laptop"}'

# on the device (admin, guardian-gated):
kosherctl portal enrol https://portal.example.org --code <code>
kosherctl portal status
```

After that `kosher-sync.timer` polls every 15 minutes, and
`kosherctl sync` pulls immediately.

## API

| method | path | who |
|---|---|---|
| POST | `/api/v1/enrol` | a device redeeming a one-time code |
| GET | `/api/v1/devices/{id}/policy` | the device itself (`X-Device-Token`) — returns a signed envelope |
| POST | `/api/v1/admin/devices` | admin: create a device + enrolment code |
| GET | `/api/v1/admin/devices` | admin: list devices, revisions, last seen |
| PUT | `/api/v1/admin/devices/{id}/policy` | admin: set policy (revision auto-increments) |
| GET | `/api/v1/admin/public-key` | admin: the portal's signing key |

## Still to build

A web UI (the API is complete enough to drive one), the remote-support
channel, and multi-tenancy.
