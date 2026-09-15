# Egress (transparent SNI intercept)

Worker TCP/443 never uses an explicit forward proxy. The vpc bridge NATs
unruled traffic to the internet; the docker host DNATs **all** vpc TCP/443
(except the vpc subnet itself) to the gateway container's vpc address `:8084`.

The gateway sniffs TLS SNI:

- **Ruled host** (a substitution rule, domain policy, or enabled exception
  names it) → MITM, scan, substitute `piso_…` / block, log.
- **Unruled host** → raw TCP splice. Real end-to-end TLS, no CA, no log.

The worker API (`http://gateway:8083`) is vpc HTTP on a non-443 port, so
informant / checkin / ports / context are never intercepted.

```
worker (default route → internet via vpc bridge masquerade)
   │
   ├─ not :443 ──────────────────────► NAT'd by Docker bridge
   ├─ :443 to vpc addresses ─────────► direct (worker API, DNS)
   └─ :443 to the internet
         └─> host DNAT → gateway:<vpc-ip>:8084
               └─> sniff SNI
                     ├─ ruled  → MITM + substitution
                     └─ unruled→ raw TCP splice
```

No `SO_ORIGINAL_DST` is needed: the gateway derives the host from the TLS
ClientHello's SNI and dials the origin itself.

## Deploy

```bash
sudo make install    # rebuilds gateway; host-nat runs on iptables hosts
piso up              # recreate workers (no proxy env)
sudo PISO_DATA=~/.piso ./scripts/transparent-egress.sh install
```

`scripts/transparent-egress.sh` looks up the gateway's vpc IP and the vpc
bridge at install time. Re-run `install` after the gateway container is
recreated (new vpc IP).

## Verification (inside a worker)

```bash
# unruled: real origin cert, no gateway hop
curl -sv https://example.com/ 2>&1 | grep issuer

# ruled: piso CA (system store already trusts it)
curl -sv https://<ruled-host>/ 2>&1 | grep issuer

# worker API still direct
curl -sS "$GATEWAY_URL/api/v1/worker/health"
```

## Tradeoffs

- Unruled traffic is not inspected, blocked, or logged.
- Intercept is IPv4 TCP/443 only (`enable_ipv6: false`). HTTP/3 (QUIC),
  :80, and raw TCP bypass.
- Ruled hosts need the piso CA (baked into the worker trust store).
- A `*` substitution rule MITMs every SNI. Keep `allowedHosts` tight.

## Rollback

```bash
sudo ./scripts/transparent-egress.sh uninstall
```
