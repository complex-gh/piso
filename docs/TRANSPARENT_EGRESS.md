# Transparent egress (CCT-style)

Moves piso from "explicit proxy + CA everywhere" to a **transparent** model
like the `claude-code-telegram` gateway: the worker's default route leads
straight to the internet, the proxy is invisible, and only *ruled* hosts are
intercepted for MITM/substitution.

## What changes (4 layers)

| Layer | Before | After |
|---|---|---|
| Worker network | `piso_vpc` is `internal: true`; egress only via the gateway's HTTP proxy env | `piso_vpc` NATs off the bridge: default route → internet directly |
| Worker env | `HTTPS_PROXY`/`HTTP_PROXY` → gateway | No proxy env. `/piso-ca.pem` stays mounted (ruled hosts still MITM'd) |
| Gateway | MITM everything on `:8080` (CONNECT) | `-transparent-listen :8084` (SNI-based): MITM ruled hosts, raw-splice unrouted |
| Host NAT | — | `scripts/transparent-egress.sh`: DNAT ruled hosts' `:443` → `127.0.0.1:8084` |

## How the pieces fit

```
worker (default route → internet via vpc bridge masquerade)
   │
   ├─ unrouted hosts ─┴─> NAT'd by Docker bridge → real end-to-end TLS, no CA, no gateway
   └─ ruled hosts  (github.com …)
         └─> host DNAT :443 → 127.0.0.1:8084
               └─> gateway transparent listener: sniff SNI
                     ├─ ruled  → MITM + substitution (existing pipeline)
                     └─ unrouted→ raw TCP splice (no CA either)
```

No `SO_ORIGINAL_DST` is needed: the gateway derives the host from the TLS
ClientHello's SNI and dials the origin itself.

## Deploy

```bash
# 1. gateway code + compose + host DNAT (transparent-egress.sh runs
#    automatically at the end of make install on iptables hosts)
sudo make install
# 2. workers now route directly; recreate them so the new env applies
piso up
```

Refresh ruled-host IPs whenever they change (github.com rotates):
`sudo PISO_DATA=~/.piso ./scripts/transparent-egress.sh refresh` (put it on
a cron or call it from `piso up`). Port customization: `piso up
--transparent-port N` (persisted to ports.json and picked up by the script).

## Verification

```bash
# in a worker: NO proxy env, NO --cacert needed for unrouted hosts
curl -sS https://api.github.com/zen          # 200 (real cert, direct NAT)
# ruled hosts still need the piso CA (they're MITM'd — that's the point)
curl -sS --cacert /piso-ca.pem https://github.com/   # 200
# gateway dashboard log: ruled requests present, unrouted absent
```

## Tradeoffs

- **Unruled traffic is not inspected, blocked, or logged** (same tradeoff as
  CCT). The old "no real secret ever leaves a worker unseen" guarantee now
  applies only to ruled hosts.
- `internal: false` on `piso_vpc` means a compromised worker has a real
  internet path by itself; rely on the worker's own hardening (cap_drop,
  read-only rootfs) for containment.
- DNAT interception is IPv4 + `:443` only (matches `enable_ipv6: false`).
- Ruled-host IP lists go stale — run `refresh`.

## Rollback

```bash
sudo ./scripts/transparent-egress.sh uninstall
# in compose/gateway.yaml: set piso_vpc internal:true + masquerade:false,
# remove PISO_TRANSPARENT_LISTEN; in worker.yaml.tmpl restore HTTPS_PROXY
sudo make install && piso up
```
