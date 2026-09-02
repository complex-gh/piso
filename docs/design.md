# piso design

## Threat model

**Attacker**: the AI agent running inside the worker, plus any code it runs,
plus prompt-injected content from repositories/docs/build output it reads.

**Assets to protect, in order**:
1. Real credentials — must never reach the worker (disk, env, memory).
2. Host filesystem — only the project mount is exposed.
3. Host network — the worker can only reach the internet through the gateway.

**Assumed trusted**: the host user, the gateway container, Docker's isolation,
and the pi process as the workload (it is the *attacker*, not the trust root —
everything it can do is bounded by the worker + gateway).

**Out of scope**: exfiltration of code/data the agent legitimately reads (an
agent with a mount + internet can always upload your source — that's inherent);
defense against a compromised host; DoS.

## Topology

```
host (you)
  │  piso CLI  ·  browser dashboard (127.0.0.1:8081)  ·  ingress (piso.local)
  ▼
GATEWAY  piso-gateway (sole egress)
  ├─ :8080  egress MITM proxy   (CONNECT → TLS terminate → scan → decide)
  ├─ :8081  control plane       (web UI, secrets/rules/patterns API, SSE log, retry)
  └─ :8082  ingress reverse proxy (name.piso.local → worker:port)
  │
  ▼
WORKER  piso-worker-<proj>  — pi on Bun
  ├─ /workspace  ← host project dir (rw, the only host mount)
  ├─ piso-agent  ← named volume for pi sessions (persist across attach)
  └─ /piso-ca.pem ← gateway CA (ro) so Bun/curl trust the MITM cert
```

Docker network `piso_vpc` is **`internal: true`**. Docker drops traffic forwarded off that bridge, so the worker has no path to the internet, LAN, or IMDS except the gateway (which is also on `piso_egress`, a normal NAT network). Masquerade is also disabled as belt-and-suspenders. This flag is the enforcement point — without it the whole design is advisory. Compose will not flip `Internal` on an already-created network; `piso up` tears down a leaky `piso_vpc` and recreates it.

Worker hardening: `cap_drop: [ALL]`, `no-new-privileges`, `--init`, read-only rootfs, `/tmp`+`/run`+`/root/.cache` as tmpfs, no docker.sock, no host `~/.ssh` or `~/.pi/agent` (bare named volume).

## Decision pipeline (egress)

For every decoded request the gateway runs, in order:

1. **Internal target?** (loopback, link-local, RFC1918, metadata) → **block** `internal-target`. EXCEPT `.piso.local` + container service names (control plane).
2. **Denied domain?** → **block** `denied-domain`.
3. **Credential-looking content?** (exact match against stored real values, OR regex library hit) → **block** `real-secret-detected` / `credential-pattern`, unless a user **exception** matches → allow.
4. **Placeholder present?** (`piso_...`) → substitute via rule `(placeholder, host)` → **substitute**; no rule → **block** `no-secret-rule` (retryable).
5. Plain request → **allow** by default.

Blocked requests return **407** to the worker with only `X-Piso-Request-Id` + `X-Piso-Reason` — never details. The gateway's log/UI hold the full story, and the request is **captured** for retry.

## Secrets policy

- Only `piso_...` placeholders ever exist in the worker.
- Real values live in the gateway's `.piso/state.json` (host-mounted, gateway-only, mode 0600) and in gateway memory.
- Substitution happens at the last hop, inside the gateway, before upstream TLS.
- The request log stores **placeholder names and redacted samples only** — never real values.
- Patterns (the "looks like a credential" library) ship with ~30 defaults and are user-extendable via the UI; a new pattern is compiled in live.

## The "block → fix → retry" loop

1. Worker request carries `piso_xyz` with no rule → 407 + captured request + log row `retryable`.
2. User adds the secret + rule (UI or `piso secrets add` + rule).
3. Click **retry** (or `POST /api/v1/requests/:id/retry`) → the gateway re-runs the **full policy fresh** on the captured request (a stale verdict is never trusted), substitutes if now allowed, forwards.

## Ingress (dev servers in the worker)

`piso expose 5173 --name preview` → `POST /api/v1/routes` → gateway reverse-proxies `https://preview.piso.local` → `worker:5173` (websocket-capable, `FlushInterval=-1` for SSE). Host browser trusts the gateway CA (install `.piso/ca.crt`) → TLS green.

## Files

```
cli/     Go CLI: piso (up/down/attach/status/secrets/expose/logs/dashboard)
gateway/ Go MITM proxy + control plane + web UI (embedded)
worker/  Docker image: node + pi, CA trust baked, hardening in compose
compose/ gateway.yaml (shared) + worker.yaml.tmpl (per-project render)
scripts/ smoke.sh: end-to-end verify of substitute/block/retry/ingress (no Docker)
         isolation.sh: worker noproxy must fail; proxy and gateway egress must work
```