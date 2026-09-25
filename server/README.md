# Technocore coordinator

Django 5.2 LTS + Channels/Daphne service for identity, fleet, encrypted blobs,
events, and WebSockets from gateways and web clients. Real secrets never
land here; gateways decrypt envelopes locally.

## Compose (EC2)

```bash
cp .env.example .env
# set DOMAIN, ACME_EMAIL, DJANGO_SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS
docker compose up --build -d
```

Caddy terminates TLS on 80/443 and proxies to Daphne. Postgres and Redis
are not published. The `worker` process consumes the `technocore` channel.

Sockets:

- `wss://$DOMAIN/ws/gateway/` — gateway auth (ed25519) then `ready` / envelope notices
- `wss://$DOMAIN/ws/client/` — logged-in browser
- `https://$DOMAIN/health/`
- `https://$DOMAIN/login/` and `/pair/<id>/` — phone approves a gateway
- `https://$DOMAIN/admin/`
- `POST /api/v1/pairing-requests` — device starts pairing
- `GET /api/v1/pairing-requests/<id>?token=` — device polls
- `GET /api/v1/sync/secrets` — signed gateway pull of ciphertext + envelopes

On the machine running piso:

```bash
piso gateway launch --server https://$DOMAIN --name "Virginia"
# open the printed /pair/<id>/ URL while logged in, Approve
piso gateway status
```

The local gateway process polls identity + envelopes from `~/.piso/technocore/identity.json` (the `PISO_DATA` mount). Local `piso up` still works unpaired.

## Layout

- `identity` — accounts, Nostr principals, memberships
- `fleet` — gateways, workers, projects, infra inventory
- `access` — resources, grants, ciphertext, key envelopes
- `work` — directives, plans, tasks, approvals, human actions
- `knowledge` — booster metadata, curator proposals, Hammerspace notes
- `events` — pairing requests and the append-only event log
