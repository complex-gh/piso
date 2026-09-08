# Plan: automatic ingress for every worker server — `name.piso.local` for any port, any worker

## Context

Today `piso expose <port> --name n` is manual: a host runs it once per server, it
POSTs a route row and appends one `/etc/hosts` line. Goal: **any** server on
**any** port in **any** worker gets a `<label>.piso.local` subdomain on the host
with no per-server ceremony, for any number of workers and servers — while the
host port map stays fixed (proxy / control / ingress = 8080 / 80 / 8082).

The architecture already makes this free, unblocked by two gaps:

- The ingress listener (`:8082`) dispatches on the **Host header** via
  `RouteByName`, NOT on port — so worker-port space is unbounded and host ports
  never grow. A route row per `(worker, port)` is all that's missing.
- `name.piso.local` only resolves if a hosts line was appended per label; labels
  today are created by hand.

This plan automates the loop end-to-end with the same trusted boundaries piso
already uses: discovery + reporting runs **inside the worker** (the only place
that can see its own listeners), and the worker→gateway channel is the vpc-only
`:8083` WorkerHandler — never the host control plane.

## Approach

Four stages, mirroring the proven `planning-watch.sh` / `context-watch.sh` pattern:

### 1. Discover (worker) — generalize the watcher into a port enumerator

New `worker/ports-watch.sh` (same shape as `planning-watch.sh`): every ~2 s read
`/proc/net/tcp` **and `/proc/net/tcp6`**, parse `LISTEN` entries
(`local_address:port`), and publish the **reachable** set:

- Only listeners bound to `0.0.0.0` (IPv4) are published — the gateway connects
  over the vpc, so a `127.0.0.1` bind is invisible to it. Loopback-only listeners
  are reported to the gateway as `note="bound to localhost — not reachable"` so
  the UI can hint, and are not routed.
- The vpc is IPv4-only (`enable_ipv6: false` in `compose/gateway.yaml`), so
  `::`/`[::]` listeners are excluded from routing too.
- No new image deps: `/proc` parsing avoids `ss`/`iproute2`, which are absent
  from the `node:*-slim` image. Follow `context-watch.sh`'s sanitization
  discipline (control-char strip, length caps) on all reported strings.

Protocol: POST the **full desired set** `{ports:[{port, note}]}` on any diff,
plus a light heartbeat (every ~10 s) so the gateway can mark routes down. The
gateway reconciles set-vs-routes, so no delete-on-first-blip churn.

### 2. Announce (worker → gateway) — generalize the pending-ingress key

Today `IngressRequestRec` is keyed by `(worker, kind)` with **one** pending row
per worker (`UpsertPendingIngress` / `CancelPendingIngress`). For N servers per
worker the key becomes **`(worker, port)`**, still `kind`-namespaced:

- New `IngressKindServer = "server"`, next to `IngressKindPlanning`. The
  planning flow (single row per worker, human-approval inbox) is untouched.
- `UpsertPendingIngress` upserts by `(worker, kind, port)`; support multiple
  pending rows per worker.
- `CancelPendingIngress` gains a port argument so a dying server cancels exactly
  its row; `planning-watch.sh` keeps the existing single-port semantics.

New worker-API endpoint on the `:8083` mux (`WorkerHandler`):
`POST /api/v1/worker/ports` — body `{worker, slug, ports:[{port,note}]}`. This
is the same trust boundary planning already uses (vpc-only, container-verified
via `workerIdentityFn`), and it is where **auto-approval** happens (§3).

### 3. Publish (host decision) — auto-create routes, keep them loud

Routes are `(name → worker:port)` rows; the browser reaching them is what
matters, and every route is visible and revocable. So **server-kind requests
auto-create their route immediately** (a request row + `ApproveIngress`-style
publish in one call), with safety + visibility rails:

- Rate cap per worker (e.g. max 20 concurrent auto-routes; `expose` aliases not
  counted) so a chatty agent can't grow `state.json` unboundedly.
- Every route row carries provenance: new `RouteRec.Origin` (`auto` | `expose`).
- Dashboard gets a **Live routes** panel (reuses the planning chip/SSE wiring):
  each route shows target `worker:port`, label URL, origin badge, and a kill
  button (DELETE `/api/v1/routes/{id}`). Joining `RouteRec.Worker` →
  `WorkerCtx` (slug/project) is free provenance.
- If the reviewer prefers stricter gating, the TOFU per-`(worker,port)` inbox is
  the existing approved-flow unchanged — same discovery/announce/hosts work, only
  the auto-approve step becomes a pending row. Recommendation: auto-create.

`piso expose <port> --name pretty` stays as the **alias** mechanism: a permanent,
human-chosen label (Origin `expose`) coexisting with the auto label for the same
target. Aliases are never auto-deleted; auto routes are (see Lifecycle).

### 4. DNS (host side) — append on demand, GC idle lines, watch for free

`/etc/hosts` has no wildcards; each label needs a line. Reuse + extend
`cli/internal/pisoconfig/hosts.go`:

- `ReconcileIngressHosts(expected []string)`: rewrite the `# managed by piso`
  block idempotently — add missing `127.0.0.1`/`::1` lines, remove labels no
  longer routed. Replaces the append-only `EnsureIngressHosts` as the `piso up`
  path (`syncIngressHosts` in `main.go` already calls it — switch to reconcile).
- New `piso sync [--watch]`: one-shot reconcile, or `--watch` consuming route
  events from the existing SSE stream (see below) and re-reconciling on delta,
  printing the sudo hint only when a write fails. This is the "subdomain appears
  the moment the server starts" host actor — ~30 lines, no daemon infra.
- **The watcher is a real daemon with managed lifecycle — no ceremony:**
  - `piso up` calls `ensureSyncWatcher()`: check `~/.piso/sync.pid` (exists +
    alive + the process is ours), and if absent, spawn `piso sync --watch`
    detached (`nohup … > ~/.piso/sync.log 2>&1 &`) and write the pidfile.
    Idempotent: a second `piso up` finds it alive and does nothing. Because
    `piso up` already runs with sudo (it writes `/etc/hosts`), the watcher it
    spawns is root too — hosts writes succeed every time, making the "sudo hint
    on write failure" path vestigial except for a hand-foregrounded watcher.
  - `make install`/`make uninstall` call `stopSyncWatcher()` (kill the
    pidfile'd process) so an orphaned watcher never writes to a hosts block
    after the install is gone. Ordering is safe: install → kill →
    `piso setup --rebuild` → next `piso up` restarts it.
  - Watch-outs, both accepted: (a) the daemon does not survive reboot — stale
    hosts lines linger harmlessly until the next `piso up` re-reconciles;
    (b) double-run safety comes from the pidfile check; even if two watchers
    ever run, the managed-block rewrite is idempotent, so no corruption.
- The `?route=` apex fallback (`ingress.go`) stays as the no-root escape hatch
  and for `piso expose` aliases; it already handles any label.

### Naming: `<slug>-<port>` — flat and deterministic

Labels must be globally unique (`ApproveIngress` → `ErrIngressLabelTaken`).
Slugs are unique per host and ports unique within a worker, so
**`<slug>-<port>.piso.local`** is collision-free by construction and
predictable. Sanitize via the existing `sanitizeDNSLabel`, guard 63 chars
(truncate from the right; slugs are already short). Planning keeps its
`plan-<slug>` scheme — separate namespace, no churn in the Plannotator flow.

### Lifecycle

- Watcher heartbeat → gateway marks route `lastSeen`; dashboard shows
  **down** when `lastSeen` is > ~45 s stale (dev servers restart constantly —
  no delete on first blip).
- Auto routes are deleted after a **down-grace** (~10 min) period; `expose`
  aliases persist until deleted (labels are sticky across worker restarts either
  way — routes live in `state.json`).
- Routes/events stream over the existing SSE endpoint: extend the stream to
  emit `route` events (there are already `record` + `planning` events via
  `SubIngress`), consumed by the CLI watcher and the dashboard live panel.

## Files to modify

- **NEW `worker/ports-watch.sh`** — `/proc/net/tcp{,6}` listener enumerator +
  diff/heartbeat reporter (bash, `planning-watch.sh` shape, `context-watch.sh`
  sanitization).
- **MOD `worker/Dockerfile`** — `COPY ports-watch.sh` + `chmod +x`.
- **MOD `worker/entrypoint.sh`** — launch `ports-watch.sh` alongside the other
  watchers (same `GATEWAY_URL`/`PISO_WORKER_NAME` guard).
- **MOD `cli/internal/workerhash/workerhash.go`** — add `ports-watch.sh` to
  `Files` (context hash drives the rebuild).
- **MOD `gateway/internal/store/store.go`** —
  `IngressKindServer`; `IngressRequestRec` gains `Kind`-scoped multi-row key by
  port; `RouteRec` gains `Origin string json:"origin,omitempty"`; bump
  `CurrentStateVersion` 3 → 4 (v3→v4 migration: default `origin:"expose"` for
  existing rows, `"auto"` for new server routes); `UpsertPendingIngress` /
  `CancelPendingIngress` port-keyed; `broadcastRoutes`/`SubRoutes` mirroring
  `broadcastIngress`/`SubIngress`.
- **MOD `gateway/internal/store/ingress.go`** — key change + auto-publish helper
  (server kind creates the route inline, rate-capped).
- **MOD `gateway/internal/server/worker.go`** (:8083) —
  `POST /api/v1/worker/ports` handler: validate worker/slug (`workerNameRe`,
  `slugFromWorker`), sanitize ports/notes, reconcile set → routes.
- **MOD `gateway/internal/server/ingress.go`** — `autoRouteName(worker, port)`
  (`<slug>-<port>`, 63-char guard); export for the worker API.
- **MOD `gateway/internal/server/server.go`** — SSE `route` events; route list
  already at `GET /api/v1/routes` (add `lastSeen`/`down` view fields);
  DELETE route already exists.
- **MOD `gateway/internal/server/ui/index.html`** — Live routes panel
  (provenance via workers context, kill button, down state), reusing chip/SSE
  patterns.
- **MOD `cli/internal/pisoconfig/hosts.go`** — `ReconcileIngressHosts`
  (add/remove inside the managed block), keep `EnsureIngressHosts` for
  single-label appends.
- **MOD `cli/cmd/piso/main.go`** — `syncIngressHosts` → reconcile; new
  `piso sync [--watch]` (SSE consumer) plus `ensureSyncWatcher` /
  `stopSyncWatcher` (pidfile + liveness, single instance); `cmdExpose` creates
  `Origin:"expose"`.
- **MOD `Makefile`** — `install`/`uninstall` targets call `stopSyncWatcher()`
  (kill pidfile'd watcher) so no orphan writes hosts after removal.
- **MOD `compose/worker.yaml.tmpl`** — no schema change needed (slug/env already
  present); verify watcher env set; document port-binding requirement.

## Reuse

- `worker/planning-watch.sh` — watcher skeleton (poll loop, curl POST to
  `$GATEWAY_URL`, env guards, no-cancel-on-drop semantics).
- `worker/context-watch.sh` — /proc scanning (python3), label sanitization,
  diff-and-post-on-change, entrypoint launch guard; the image has no `ss`.
- `gateway/internal/server/worker.go` — WorkerHandler `:8083` trust boundary;
  `handleWorkerGetPlanning` shape for the new list endpoint.
- `gateway/internal/store/ingress.go` — `UpsertPendingIngress` /
  `ApproveIngress` / `CancelPendingIngress`; the `ErrIngressLabelTaken` guard is
  the collision proof for `<slug>-<port>`.
- `gateway/internal/server/server.go` — SSE `stream()` (`SubIngress` pattern),
  `ingress()` Host-header dispatch (`RouteByName`) — the reason host ports
  never grow.
- `cli/internal/pisoconfig/hosts.go` — `EnsureIngressHosts`, `ingressFQDN`,
  `hostsMarker`; `cli/internal/pisoconfig/ports.go` — `ingressPublicURL` /
  `LoadHostPorts().Ingress` for label URLs with a non-80 ingress port.
- `gateway/internal/server/ui/index.html` — planning chip + approve/dismiss
  modal patterns for the live-routes panel.

## Steps

- [ ] **1. Worker watcher** — write `worker/ports-watch.sh`; container-test
      (`docker exec`): bound-port detection, localhost-only exclusion,
      heartbeat, diff-dedup.
- [ ] **2. Build plumbing** — Dockerfile COPY, entrypoint launch,
      `workerhash.Files` entry.
- [ ] **3. Gateway store** — `IngressKindServer`, port-keyed pending rows,
      `RouteRec.Origin`, v3→v4 migration, route broadcast/Sub.
- [ ] **4. Worker API** — `POST /api/v1/worker/ports`; set-reconcile
      (create/update/delete auto routes), rate cap, `lastSeen` updates.
- [ ] **5. Naming/URLs** — `autoRouteName`; live-route view fields; SSE
      `route` events.
- [ ] **6. Host DNS** — `ReconcileIngressHosts` + GC; `piso sync [--watch]`
      (SSE consumer); `ensureSyncWatcher` in `piso up` (pidfile + liveness,
      nohup/`~/.piso/sync.log`), `stopSyncWatcher` wired into the
      Makefile `install`/`uninstall` targets; reconcile on `piso up`;
      expose → `Origin:"expose"`.
- [ ] **7. Dashboard** — Live routes panel (provenance, kill, down state).
- [ ] **8. Tests** — store: upsert-by-port, cancel-by-port, origin defaulting,
      migration; worker API: validation + reconcile; hosts: reconcile
      add/remove idempotence; CLI sync watch smoke; daemon lifecycle
      (start idempotent, stop on install, stale pidfile recovery).
- [ ] **9. End-to-end** — two workers, multiple ports each (see Verification).

## Status

Implemented (steps 1–8). One deviation from the step-3 sketch: auto routes are
published **directly** as `RouteRec{Origin:"auto"}` rather than via extra
`IngressRequestRec` rows — a request row per port would pollute the host
approval inbox, and the route row itself (Origin+LastSeen) is the provenance.
Keep-alive/GC: the watcher posts the full set; stale auto routes are swept in
`Routes()` (lazy, on read) after a 10-min grace.

Follow-up (host DNS lifecycle): the one-shot hosts reconcile at `piso up` only
covers routes that exist at that moment, and `/etc/hosts` needs root. Added a
global hosts-sync **daemon** (`piso sync daemon`, launchd on macOS with
RunAtLoad + KeepAlive, nohup+pidfile fallback elsewhere):

- `piso up` (any project) converges on the one daemon — running → leave it,
  stopped → (re)start it (may prompt for sudo).
- `piso sync daemon-status` is pidfile + `ps` probe, no privileges.
- `sudo make install` stops/restarts it with the new binary; `make uninstall`
  stops it.
- The daemon is the `piso sync daemon` body: blocks until the gateway is up,
  reconciles once per (re)connect (SSE), then each route event.

Follow-up (bare-subdomain UX, Option B — merged web port): instead of
redirecting bare labels to a separate ingress port, the gateway now serves
control plane AND ingress from ONE published port (default 80) via a new
`WebHandler` that dispatches by Host: apex → dashboard/API; any *.piso.local
subdomain (or apex ?route=/cookie) → worker proxy. Apex/localhost/127.0.0.1
without a route signal → dashboard. The old 8082 ingress listener and
`IngressHandler` remain for backward compatibility; the redirect helper was
removed. URL generators are portless: compose injects PISO_INGRESS_PORT =
${PISO_CTRL_PORT:-80} (the public web port) into the container, so
`http://piso.local` and `http://<label>.piso.local` both work with no port.

Contribution history: (1) auto-expose steps 1–8 + tests; (2) global hosts-sync
daemon lifecycle (launchd/nohup, piso up convergence, make install/uninstall);
(3) -1 the redirect; (4) fixed stdlib field-vs-method bug (r.RequestURI).

Regression found & fixed (stale-cookie 404): with the merged web port, a
`piso_route` cookie set by the OLD apex-?route flow could hijack plain
`piso.local` into the ingress (stale label → 404 dashboard). Fix: the apex is
ALWAYS the dashboard unless an explicit ?route= is present; an explicit
?route= now redirects to the canonical portless subdomain
(http://<label>.piso.local[<webport>]<path>) instead of setting a cookie on
the apex. No cookie is read for dispatch on the apex; the legacy 8082
listener still honors a cookie (backward compat).

Follow-up (routes UX): URL/open use the canonical portless subdomain
(http://<name>.piso.local). The kill button is replaced by an on/off toggle
(RouteRec.Disabled + POST /api/v1/routes/{id}/disabled): off blocks proxying
(403) while keeping the row re-enableable. Auto-disappearance: the routes
 table polls every 5s (ensureRoutesPoll) so a stale auto route vanishes the
moment the gateway sweeps it (after the 10-min grace), with the live/down
badge flipping after 45s of no heartbeat.

Verification requires the host (no Go toolchain or Docker in the workspace
container): `go test ./...`, `sudo make install`, two `piso up` workers,
`piso sync --watch`, and curl checks per the checklist below. In-container I
validated bash (`bash -n`) and the watcher's /proc parsing (reachable vs
loopback/ULA classification) and its payload+d edup/heartbeat loop.

## Verification

- `go vet ./... && go test ./...` on the host, then `sudo make install` +
  `piso setup --rebuild`.
- `piso up` in repo A → `python3 -m http.server 8080` in worker A;
  `piso up` in repo B → `python3 -m http.server 9000` (and 9090) in worker B.
  Within ~10 s: `GET /api/v1/routes` shows
  `a-8080` → `piso-worker-a:8080`, `b-9000` → `piso-worker-b:9000`,
  `b-9090` → `piso-worker-b:9090`; `/etc/hosts` has the three labels
  (auto-added by the daemonized `piso sync --watch` started from `piso up`);
  `curl b-9090.piso.local` serves worker B's server.
- Kill one server → route shows **down** in the dashboard within ~45–60 s;
  it is NOT deleted on the blip; server restarted → recovers via heartbeat.
- Stop a worker's ports-watch / `docker stop` the worker → auto routes GC'd
  after down-grace; `piso expose --name pretty` alias survives.
- Two workers → same-label collision impossible (distinct slugs); 63-char
  label truncation produces valid DNS.
- Existing `~/.piso/state.json` (v3) migrates to v4; planning flow still
  works (one row per worker, host approval, `plan-<slug>` labels).
- Daemon lifecycle: `piso up` starts the watcher (second `piso up` while
  running is a no-op, pidfile correct); `sudo make install` stops it and the
  next `piso up` restarts it; `uninstall` leaves no orphan watcher. Reboot
  leaves stale hosts lines that heal on the next `piso up`.