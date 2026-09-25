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

- `wss://$DOMAIN/ws/gateway/`
- `wss://$DOMAIN/ws/client/`
- `https://$DOMAIN/health/`
- `https://$DOMAIN/admin/`

## Layout

- `identity` — accounts, Nostr principals, memberships
- `fleet` — gateways, workers, projects, infra inventory
- `access` — resources, grants, ciphertext, key envelopes
- `work` — directives, plans, tasks, approvals, human actions
- `knowledge` — booster metadata, curator proposals, Hammerspace notes
- `events` — pairing requests and the append-only event log
