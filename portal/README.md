# Portal (stage 5 — not built yet)

Self-hostable, single-tenant FastAPI app. Two jobs:

1. **Remote filter configuration** — a web UI over the same policy document
   the device stores at `/var/lib/kosher/policy.json`
   (schema: `../policy/schema/policy.schema.json`).
2. **Remote support access** — design TBD.

## Device sync contract (draft)

- Device enrolls once with a short-lived token → receives a device ID + the
  portal's policy-signing public key.
- A sync agent on the device long-polls `GET /api/v1/devices/{id}/policy`;
  responses are signed policy documents (`source: "portal"`, higher `revision`).
- Local changes made through kosherd bump `revision` with `source: "local"`;
  the portal treats the device copy as authoritative for merge conflicts it
  didn't cause (local-first model).
- The device NEVER exposes an inbound port; everything is device-initiated.
