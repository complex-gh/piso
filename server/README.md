# Technocore coordinator

Django 5.2 LTS + Channels/Daphne. Identity, fleet, encrypted blobs, events,
and WebSockets from gateways and web clients. **Real secrets never land here.**
Gateways wrap and unwrap locally (`chacha20poly1305-v1`).

Two-machine enrollment (DNS, `.env`, QR, `piso gateway launch`):
[docs/technocore-setup.md](../docs/technocore-setup.md).

## Compose (deployed host)

Caddy owns host **80/443**. Do not run this on a machine that also binds
`piso.local` on port 80.

```bash
cp .env.example .env
# set DOMAIN, ACME_EMAIL, DJANGO_SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS
docker compose up --build -d
docker compose exec server python manage.py createsuperuser
```

Staff: `https://$DOMAIN/setup/` or `/admin/` for Account + membership.
Then `https://$DOMAIN/login/` on the phone.

Postgres and Redis are not published.

## Enrollment

On a machine running piso (laptop gateway):

```bash
piso up
piso gateway launch --server https://$DOMAIN --name laptop
```

Scan the terminal QR (URL is `https://$DOMAIN/pair/<id>/` only). Compare the
fingerprint, then Approve.

Poll uses header `X-Piso-Poll-Token`, not a query string.

## Share a secret

The local gateway wraps plaintext and POSTs ciphertext:

```bash
piso secrets add mykey piso_mykey 'sk-...' api.example.com
piso secrets publish piso_mykey
# or:
printf '%s' 'sk-...' | piso secrets share --name mykey --placeholder piso_mykey --hosts api.example.com
```

`GET /api/v1/secrets` returns metadata only. `GET /api/v1/sync/secrets` is
gateway-authenticated and returns ciphertext + wraps for that principal.

## HTTP

- `wss://$DOMAIN/ws/gateway/` — bound Ed25519 auth, then `ready` / `envelopes_changed`
- `wss://$DOMAIN/ws/client/` — logged-in browser
- `https://$DOMAIN/health/`
- `https://$DOMAIN/login/` `?next=/pair/<id>/`
- `https://$DOMAIN/setup/` — first account (staff)
- `POST /api/v1/pairing-requests`
- `GET /api/v1/pairing-requests/<id>` + `X-Piso-Poll-Token`
- `GET /api/v1/gateways`
- `POST /api/v1/secrets` — ciphertext + wrap blobs only
- `GET /api/v1/sync/secrets`
