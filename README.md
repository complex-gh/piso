# piso

Isolate an AI coding agent (pi) inside a Docker worker. Egress TCP/443 is
intercepted by a MITM gateway that substitutes fake credentials for real ones
and blocks anything that looks like a real credential. Unruled :443 is spliced
as real end-to-end TLS.

```
you (terminal: piso / host browser)
   │                 │  1 published port on gateway
   ▼                 ▼
GATEWAY  ── control plane (UI: secrets, patterns, log; API: expose, retry)
   │  egress  vpc :443 → SNI intercept (MITM ruled / splice unruled)
   │  ingress  (name.piso.local → worker:port)
   ▼
WORKER (pi, Bun)  ── named volume ~/.pi/agent (sessions persist)
   └─ mount: project dir (rw)
```

## Core guarantees

- **The worker never holds real credentials.** Only `piso_...` placeholders.
  Real values live in the gateway's secrets file (host-mounted, gateway-only).
- **Ruled HTTPS is inspected.** A request that carries a real credential — or
  anything that *looks* like one — is blocked and flagged. Detection uses a
  regex library (plus exact matches against known real values).
- **Blocked requests are first-class objects** with an id, a captured request,
  and a retry button: add a secret/exception, retry, done.
- **Unruled :443 is spliced.** Real origin certs, no log, no CA. Non-443
  traffic NATs off the vpc bridge.
- **Ingress is gated.** The only way onto a worker port is the gateway's
  reverse proxy (`*.piso.local`).

## Layout

```
cli/     Go CLI: piso          (up, attach, expose, logs, secrets, dashboard)
gateway/ Go SNI intercept + control plane + request log + web UI
worker/  Docker image: pi/Bun + CA trust + hardening
compose/ docker-compose (worker + gateway, piso_vpc)
```

## Quick start

```bash
git clone <this-repo> && cd piso
make install                 # sudo if PREFIX=/usr/local is not writable
# or: make install PREFIX=$HOME/.local   # then ensure ~/.local/bin is on PATH
cd /path/to/your/project
piso up          # ensure gateway + worker for the current dir
piso attach      # enter the worker, run pi
piso attach --new  # start a fresh session instead of restoring
piso attach --session <id>  # open a specific session
piso dashboard   # open http://piso.local
piso expose 5173 --name preview   # reverse-proxy a worker dev server
```

`make install` (including `sudo make install` over an existing copy) replaces `$(PREFIX)/bin/piso` and `$(PREFIX)/share/piso`, then runs `piso setup --rebuild` as the login user. That imports any leftover repo `.piso` files that `~/.piso` does not already have, rebuilds the gateway image, recreates the container, and migrates `state.json` on startup. Live secrets in `~/.piso` are kept. Gateway secrets/CA/logs live in `~/.piso` (`PISO_DATA` overrides). Override the share tree with `PISO_HOME`. Secrets filled in the dashboard **Blocked secrets** modal are stored in the gateway; the next `piso attach` exports placeholders only (`ANTHROPIC_API_KEY=piso_…`) from `~/.piso/placeholders.env`. Existing workers need one `piso up` to mount that file.

The dashboard is **http://piso.local** (host port 80 → container 8081). `piso up` adds `127.0.0.1 piso.local` to `/etc/hosts` when it can; otherwise it prints the line to add. If port 80 is taken:

```bash
piso up --ctrl-port 8081    # then http://piso.local:8081
```

`--ingress-port` works the same way. A taken port is a hard error, not a silent remap. Chosen ports are saved in `~/.piso/ports.json`.

On iptables hosts, `make install` also installs vpc `:443` DNAT
(`scripts/transparent-egress.sh`). Re-run that script after the gateway
container is recreated (new vpc IP).

### Hosts sync daemon (auto subdomains)

`piso up` also converges the host on a global hosts-sync service so every
**<slug>-<port>.piso.local** resolves the moment a worker server starts — no
manual `piso expose` or hosts lines. It is managed as root (`sudo make install`
restarts it with the new binary; `make uninstall` stops it):

```bash
piso sync daemon-status    # running? (no sudo needed)
piso sync daemon-restart   # (re)install/start; piso up does this automatically
```

On macOS it is a launchd service (`com.piso.sync`, survives reboots); elsewhere
a nohup+pidfile fallback is used. The daemon only needs `/etc/hosts` write
access and the gateway's control-plane port — workers do not need to run for it
to stay alive.

See `docs/design.md` for the threat model and the decision pipeline.
