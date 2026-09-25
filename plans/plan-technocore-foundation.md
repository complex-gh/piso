# Technocore foundation — pairing, identity, envelopes

Do not add product surface (worker launch, Boost, Hammerspace, Locutus) until
this layer is real. Envelope grants, remote workers, and provider proxies all
assume it.

Related: `plans/plan-technocore.md` (architecture overview).

Work in `internal/technocore` (Go) and `server/app/core/envelope.py` (Python) must stay byte-compatible.

## Already shipped

- Shared Go package `piso/internal/technocore`: identity, request-bound auth, envelope wrap/open
- Matching Python `core/envelope.py` and `core/crypto.py`
- Identity file: signing Ed25519 + encryption X25519, legacy field migration
- Auth: `piso-gateway-auth/v1` bound to method, path, body hash, nonce; nonce cache
- Pairing: hashed poll token (header, not query), rate limit, idempotent re-approve, account picker, encryption pubkey, `expire_pairings`
- Gateway WebSocket uses the same auth scheme; backoff resets after a session that reached `ready`
- Envelope open on sync uses wrap blobs (`chacha20poly1305-v1`)
- Tests: Django pairing/crypto; Go identity/auth/envelope (run with `go test ./internal/technocore`)

## Remaining on this layer

- See `plans/plan-technocore-enroll-share.md` for enrollment QR and secret sharing (in progress in tree).
- `Principal.nostr_pubkey` is still Ed25519 device id (rename when Nostr events exist)
- `make install` must copy `internal/` into the share tree

## After grants work

- `piso worker launch` using the same pairing kind
- Boost / Hammerspace as gateway backends (workers never see those OAuth tokens)

## Default production story (unchanged)

```text
sudo make install          # local-only still works
piso up && piso attach
# later:
piso gateway launch --server https://$DOMAIN
# phone, logged in: open /pair/<id>/ and Approve
```

The gateway must keep working offline if unpaired. Technocore is optional.

## Crypto non-goals for this document

- Putting OAuth plaintext on `server.com`
- Worker-held provider credentials
- nVPN / mesh
- Treating Ed25519 enrollment keys as Nostr relay identities without a spec
