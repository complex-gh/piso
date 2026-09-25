# Gateway enrollment and secret sharing

Foundation crypto, pairing, and sync-open already exist
(`plans/plan-technocore-foundation.md`). This plan finishes **enrollment UX**
(including QR) and **account-wide secret sharing** without putting plaintext
on `server.com`.

Do not implement Boost, Hammerspace, or worker launch in this pass.

## Trust rules

- Server stores ciphertext, wrap blobs, metadata, grants, and events only.
- Wrapping and unwrapping happen on **user devices and gateways** (`piso/internal/technocore`).
- Django must not import envelope seal/wrap/open for production paths.
  `core/envelope.py` is not a grant implementation; remove it from the server
  app or confine it to an optional offline test helper. Auth stays Ed25519
  verify only (`core/crypto.py`).
- Workers never receive wraps or plaintext vault keys.
- Revoking a wrap stops **future** sync. A gateway that already decrypted a
  value still has it until the underlying credential is rotated.

Algorithm (already implemented): `chacha20poly1305-v1`
(RFC 8439, 12-byte nonce, AAD `piso-secret-v1`; wrap version byte `0x01`,
ephemeral X25519, HKDF-SHA256 info `piso-envelope-wrap-v1`).

---

## A. Enrollment (QR)

### Current

`piso gateway launch` prints `https://$DOMAIN/pair/<id>/`. Phone must be
logged in (Django session). Poll uses `X-Piso-Poll-Token`. Re-approve is
idempotent. Encryption pubkey is stored on approve.

### QR

The QR encodes **only** the approve URL:

```text
https://$DOMAIN/pair/<uuid>/
```

It must not contain `poll_token`, private keys, or the signing secret.
Terminal still prints the fingerprint; the pair page already shows it for
comparison.

Implementation:

- CLI: render a terminal QR (e.g. `github.com/skip2/go-qrcode`) plus the URL
  and fingerprint. `--no-qr` prints URL only (logs, SSH without UTF-8).
- Pair page: if the session is anonymous, redirect to `/login/?next=/pair/<id>/`
  so a scan on a logged-out phone still lands in the right place after login.
- HTTPS only in production (`DOMAIN` + Caddy). HTTP is for local server
  development.

### Account bootstrap (thorough, not a stub forever)

Enrollment assumes an account. Keep admin provisioning, then add:

1. First-run: if no `Account` exists, authenticated staff/superuser can
   create one from a simple `/setup/` page (name → Account + owner membership).
2. Later: invite/membership APIs. Not required to share secrets among
   gateways of an existing account.

Document in `server/README.md`: `createsuperuser`, `/admin/` Account +
membership, then `/login/` on the phone. Fix the stale `?token=` poll docs
to `X-Piso-Poll-Token`.

### Gateway process

`piso up` must be running so `technocore.Run` can finish pairing poll and
open the WebSocket. `piso gateway launch` should say so if it cannot see a
healthy local gateway, but must not fail pairing if the operator will start
it next.

Local unpaired `piso up` remains fully functional.

### Enrollment tests

- QR payload is the approve URL only (unit).
- Anonymous GET `/pair/<id>/` → login with `next`.
- Poll without header 401; with header 200.
- Re-approve same device pubkey → same Principal.

---

## B. Secret sharing

Sharing is **grant + wrap**, not “upload the password to Django.”

### Recipients

On approve we already write `PrincipalKey` purpose `encryption`. Expose it:

```http
GET /api/v1/gateways
Authorization: session or gateway-auth
```

Response (no secrets):

```json
{
  "gateways": [
    {
      "principal_id": "...",
      "name": "Virginia",
      "status": "active",
      "encryption_pubkey": "<32-byte hex>",
      "fingerprint": "aabbccddeeff"
    }
  ]
}
```

Only principals on the caller’s account. Inactive/revoked omitted.

### Grant API (server is a blob store)

```http
POST /api/v1/secrets
```

Body (all ciphertext):

```json
{
  "name": "Anthropic",
  "metadata": {
    "placeholder": "piso_anthropic_prod",
    "envKey": "ANTHROPIC_API_KEY",
    "allowedHosts": ["api.anthropic.com"]
  },
  "algorithm": "chacha20poly1305-v1",
  "nonce": "<b64>",
  "ciphertext": "<b64>",
  "content_hash": "<hex sha256 of plaintext>",
  "wraps": [
    { "principal_id": "<gateway uuid>", "wrapped_data_key": "<b64 wrap blob>" }
  ]
}
```

Server:

1. Authenticate (session user with owner/admin membership, **or** enrolled
   gateway on that account via existing bound auth).
2. Validate algorithm, wrap blob lengths, principal_ids belong to the account
   and have an encryption key.
3. Create `Resource` (kind secret) + `EncryptedObject` + `KeyEnvelope` rows.
4. Append `Event` `secret.granted` with **names and principal ids only**.
5. `group_send` `envelopes_changed` to `gateway.<principal_id>` for each wrap.
6. Return `{ "resource_id", "content_version" }`. Never echo ciphertext
   unnecessarily; never echo plaintext.

Updates:

```http
PUT /api/v1/secrets/<resource_id>
```

New nonce/ciphertext/content_hash/`content_version++`, replace wraps for the
posted recipient set. Notify all previously and newly wrapped gateways
(removed recipients get `envelopes_changed` so they drop the object on next
sync).

```http
DELETE /api/v1/secrets/<resource_id>/envelopes/<principal_id>
```

Remove one wrap, notify that gateway.

```http
GET /api/v1/secrets
```

Metadata only: name, placeholder, envKey, hosts, version, recipient principal
ids. No ciphertext.

Reject bodies that include a `value` / `plaintext` field.

### Wrapping clients (where DEKs are born)

Implement wrap **once** in Go (`SealPayload` + `WrapDEK` already exist).

**1. CLI (primary)**

```bash
piso secrets share --name Anthropic --env ANTHROPIC_API_KEY \
  --hosts api.anthropic.com --placeholder piso_anthropic_prod
```

Reads the value from stdin (or `--file`), loads local identity, GET gateways,
wraps for **all active gateways** on the account (flag `--gateway fingerprint`
to subset), POST `/api/v1/secrets`. CLI authenticates as the **local gateway**
(bound auth), which is already enrolled.

Also:

```bash
piso secrets publish <placeholder>
```

Takes a secret already in the **local** vault (`state.json`), wraps that
plaintext for other gateways, POSTs. This is how a laptop that already has
keys in piso shares them to Virginia without re-typing.

**2. Web client (later in this same effort, not a toy)**

Logged-in browser: form for name/hosts/value. JS or a small WASM/Go-wasm
build of wrap is acceptable; do **not** POST plaintext to Django. Until JS
crypto is reviewed, shipping CLI publish/share is enough to make sharing
real. The HTML dashboard can still **list** metadata and revoke wraps.

### Gateway sync (already mostly written)

On `ready` and `envelopes_changed`: GET `/api/v1/sync/secrets`, `OpenEnvelope`,
upsert `SecretRec` + `SyncRulesForSecret`.

Extend sync to **remove** local `tc_<resource_id>` secrets whose resource is
absent or no longer wrapped to this principal (so DELETE wrap takes effect
on the next pull). Log removals; do not log plaintext.

If open fails, skip that object and keep going (one bad wrap must not stall
the vault).

### Notify

`GatewayConsumer.envelopes_changed` already exists. Grant/PUT/DELETE must
`channel_layer.group_send("gateway.<id>", {"type": "envelopes_changed"})`.
Integration test: mock layer or locmem, POST grant, assert a send.

### Versioning and rotation

- `content_version` monotonic per resource.
- Sync applies only wraps matching current `EncryptedObject.content_version`
  (already filtered in `list_envelopes`).
- Rotation: PUT new plaintext wrap; old ciphertext is replaced. Gateways that
  miss the notify still have the old value until they sync — document that.
- Strong revoke = rotate the **upstream** API key, then PUT.

---

## C. Operator UX

Local gateway dashboard (Go UI at piso.local):

- Technocore: not enrolled | pending (fingerprint + URL) | connected
  (server URL, last sync time, envelope error count)
- Do not display plaintext of synced secrets beyond the existing secrets UI
  (same as today’s vault).

Server `/` page: link to login, list pending pairings for the account
(optional; `/pair/<id>/` remains the scan target).

---

## D. Tests and interop

- Go: wrap/open, wrong recipient, tamper (exists). Add CLI share against a
  fake HTTP server.
- Django: POST secret without wraps 400; wrap for foreign principal 403;
  happy path creates rows and notifies; GET list has no ciphertext; sync GET
  as gateway returns only that principal’s wraps.
- Golden vector: one JSON file `internal/technocore/testdata/envelope_v1.json`
  produced by Go test `-update` or a tiny Go helper; Python tests **must not**
  be required for production. If Python envelope helper remains, it only
  checks it can open that vector — or delete the helper.

---

## E. Docs to update in the same pass

- `server/README.md`: poll header, QR, grant API, “server does not encrypt.”
- `plans/plan-technocore-foundation.md`: remaining = this plan, not a second
  crypto rewrite.
- `piso gateway launch` help text: fingerprint + QR + login URL.

---

## Order of work

1. Docs + pair `next=` redirect + CLI QR (`skip2/go-qrcode` via `go get`).
2. `GET /api/v1/gateways` (encryption pubkeys).
3. `POST/PUT/DELETE/GET /api/v1/secrets` + `group_send`.
4. Sync: delete local copies when wrap is gone.
5. `piso secrets share` / `publish` using Go wrap + gateway auth.
6. Dashboard chip on the local gateway UI.
7. Golden vector + Django tests for grant/notify.
8. Web wrap form only after 1–7 are green.

## Done when

1. Phone scans a terminal QR, logs in if needed, compares fingerprint, approves.
2. Gateway WebSocket reaches `ready` without extra ceremony.
3. `piso secrets publish piso_anthropic_prod` (or `share`) results in a second
   enrolled gateway inserting the same placeholder into its vault, with no
   plaintext on the server (DB and logs).
4. Removing a wrap causes the other gateway to drop that `tc_*` secret on
   the next `envelopes_changed` / pull.
5. Unpaired `piso up` is unchanged.
