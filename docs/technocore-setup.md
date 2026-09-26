# Technocore: coordinator + one gateway

Two machines, one public hostname. The coordinator owns host ports **80 and
443**. The gateway is a normal piso install on a laptop (or any other host).

Do **not** run piso on the coordinator: both want port 80.

## Machine A — coordinator (`https://your.domain`)

### DNS and firewall

- `A` / `AAAA` for `your.domain` → this host
- Inbound **22, 80, 443** only
- Postgres and Redis stay unpublished

### Install

```bash
git clone <piso-repo> && cd piso/server
cp .env.example .env
```

Edit `.env`:

```bash
DOMAIN=your.domain
ACME_EMAIL=you@your.domain
DJANGO_SECRET_KEY=<openssl rand -base64 48>
DJANGO_DEBUG=0
POSTGRES_PASSWORD=<strong>
ALLOWED_HOSTS=your.domain
CSRF_TRUSTED_ORIGINS=https://your.domain
```

```bash
docker compose up --build -d
curl -sf https://your.domain/health/
docker compose exec server python manage.py createsuperuser
```

Caddy obtains a Let’s Encrypt certificate for `DOMAIN`.

### First account

In a browser:

1. `https://your.domain/login/`
2. `https://your.domain/setup/` (staff user) — create the Account

Alternatively: `/admin/` → Account + Account membership (owner).

## Machine B — piso gateway

Docker plus a **current** piso from this tree (`make install` or
`PREFIX=$HOME/.local`) so the share tree includes `internal/`.

```bash
cd /path/to/a/project
piso up
piso gateway launch --server https://your.domain --name laptop
```

Compare the **fingerprint** with the pair page. Scan the terminal QR or open
`https://your.domain/pair/<id>/` (log in if asked). **Approve**.

```bash
piso gateway status
```

Expect `status: active`. The local dashboard chip should read
`technocore: active` plus the fingerprint.

Gateway logs (container): `technocore: websocket ready`.

The gateway container dials `https://your.domain` through `piso_egress`. No
`host.docker.internal` and no extra `/etc/hosts` entries.

## Optional: share a secret

On the gateway machine, after enrollment:

```bash
piso secrets add demo piso_demo 'test-value' api.example.com
piso secrets publish piso_demo
```

The coordinator stores ciphertext and wrap blobs only. This gateway unwraps
into its local vault. A second enrolled gateway would receive the same
placeholder on `envelopes_changed`.

## Checklists

**Coordinator**

- [ ] DNS points at the host
- [ ] `https://your.domain/health/` returns `{"status": "ok"}`
- [ ] Superuser + Account + owner membership
- [ ] Phone can open `https://your.domain/login/`

**Gateway**

- [ ] `piso up` is running
- [ ] `piso gateway launch --server https://your.domain`
- [ ] Fingerprints match; pairing approved
- [ ] `piso gateway status` → `active`
- [ ] Logs: `technocore: websocket ready`
