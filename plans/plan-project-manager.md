# Plan: project manager — activity informants, a blind monitor worker, and a timeline board

## Context

piso isolates an AI agent per project worker behind a MITM gateway. Two facts
make a "project manager" possible on top of it:

1. **Activity exists only inside workers.** The agent's doing (progress,
   milestones, blockers, reminders) is semantically meaningful only where the
   agent is — the gateway sees raw egress rows, not intent. So the activity
   plane must be *worker→gateway*: an **informant** in each worker summarizes
   and delivers.
2. **A monitoring/PM agent must itself be a piso citizen.** It reads activity
   and *decides* pokes; that requires an LLM, and per the threat model an LLM
   with real credentials or host access is exactly what piso prevents. So the
   monitor is a **special worker with the same rules** — blind by topology,
   scoped credentials, no project mount — that requests a scrubbed feed over a
   **privileged worker API** and writes its results back.

End state: the **dashboard becomes a PM board** (its new main screen) — one
horizontal track per project, timeline items color-coded by kind (progress /
milestone / reminder / poke / active), with an "ongoing work" indicator per
live worker.

## Approach

Five separable parts (each testable alone):

```
worker A ── informant (pi plugin/prompt) ──> activity events ─┐
worker B ── informant ─────────────────────> activity events ─┤
                                                              ▼
                                                  GATEWAY activity store
                                               (SQLite, scrubbed, SSE-broadcast,
                                                privileged worker API:
                                                POST /activity, GET /activities)
                                                              ▲
monitor worker (gateway-compose sibling, pi, no mount,          │
   scoped model key) polls GET /activities ── LLM ──────────────┘
        │  results POST back (kind=poke|reminder)
        ▼
   dashboard board tab renders tracks + timeline (default screen)
```

### 0. Monitor lifecycle: a sibling service in the gateway compose

The monitor is **host-scoped** (watches all projects), so it belongs in the
shared gateway compose — not any project worker compose. This gives "started
with the gateway, managed alongside it" for free:

```
compose/gateway.yaml:
  services:
    gateway:     (existing)
    monitor:     (new sibling)
      image: piso-worker                      # same hardened image
      container_name: piso-worker-monitor
      restart: unless-stopped                 # same lifecycle as gateway
      networks: [vpc]                         # internal; LLM egress via MITM
      environment:
        GATEWAY_URL: http://gateway:8083
        PISO_WORKER_NAME: piso-worker-monitor
        PISO_WORKER_SLUG: monitor
        PISO_ROLE: monitor
      volumes:
        - piso-agent-monitor:/root/.pi/agent   # own session volume; NO project mount
```

- Same `name: piso` compose project → `docker compose up/down` controls
  gateway + monitor together; `piso up`/`piso setup`/reboot lifecycle shared.
- `workerIdentityFn` resolves the monitor's VPC IP → slug `monitor`
  naturally, so it participates in the worker API exactly like any worker.
- **Identical sandboxing**: cap_drop, no-new-privileges, read-only rootfs,
  internal vpc, gateway-is-only-egress. No exceptions because the slug is
  "special" — the slug only names it; the sandbox is the same.
- `Internet` kill-switch applies (default off until the scoped LLM key is on).

### 1. Activity store (SQLite) + privileged worker API

**Database: `modernc.org/sqlite` (pure-Go, no CGO).** First external dependency
in the repo (go.mod gains it; vendored). Single-file DB
`~/.piso/activities.db` (host-mounted into the gateway like state.json),
initialized/migrated by the store at boot:

- Schema (idempotent `CREATE TABLE IF NOT EXISTS` + `PRAGMA journal_mode=WAL`):
  ```sql
  CREATE TABLE activities (
    id          TEXT PRIMARY KEY,
    worker      TEXT NOT NULL,            -- container name
    slug        TEXT NOT NULL,            -- source project slug
    kind        TEXT NOT NULL,            -- progress|milestone|reminder|poke|active|note
    target_slug TEXT,                     -- for pokes/reminders: which project's track
    text        TEXT NOT NULL,
    ts          INTEGER NOT NULL          -- unix ms
  );
  CREATE INDEX idx_activities_slug_ts ON activities(slug, ts);
  CREATE INDEX idx_activities_target_ts ON activities(target_slug, ts);
  ```
- Store API: `InsertActivity(Activity)`, `QueryActivities(filter)`
  (by slug / target_slug / kind / ts range / limit, SQL), `DeleteOwnActivity(id,
  worker)`. After insert: `broadcastActivity` → `SubActivities()` (SSE).
- **No `state.json` version bump** — activities are out-of-band in SQLite.
- **Worker API (`:8083`, WorkerHandler):**
  - `POST /api/v1/worker/activity` — own activity; validate `workerNameRe` +
    slug, sanitize `text` (control-char strip + cap, like `sanitizeLabel`),
    fill `ts`. **Scrub guarantee: no headers/bodies ever.**
  - `GET /api/v1/worker/activities` — the feed; same boundary as
    context/ports (vpc-only, worker-IP-identity-checked).
  - `POST /api/v1/worker/activity/revoke` — remove own row (id + worker
    scoped).
- **Control plane** (host board): `GET /api/v1/activities` mirroring
  `/api/v1/routes` (human → host-only).

**Security:** the slug is *not* trusted as identity alone — the worker API
already binds the caller's VPC IP → registered slug (`WorkerByIP`), so the
monitor's "special slug" is established by the trust boundary, and the API
contract is: any registered worker may read the feed and write its own rows,
exactly like every other worker. No `IsMonitor` flag, no name special-casing.

### 2. Enforce `Secret.Workers` scoping (monitor dependency)

`SecretRec.Workers` is stored and exposed but **never consulted** during
substitution. The monitor needs a **scoped** model key (its own LLM egress)
without being able to substitute other workers' secrets:

- In `policy.go` (`substituteAll` / `lookupRule`), when a placeholder resolves
  to a secret whose `Workers` is non-empty, require the request's **origin
  worker** (available via `workerIdentityFn` / `h.workerID`) to be in that
  list; else no substitution → `no-secret-rule` block (retryable).
- Backward compatible: empty / `*` = all workers.
- Monitor key: `piso secrets add ... --workers piso-worker-monitor` → its LLM
  calls pass; a plain worker sending the same placeholder is blocked.

### 3. Informant (in-worker semantic activity)

Two tiers, both via the worker API:

- **Tier A — dumb informant:** `worker/activity-watch.sh` (planning/context-watch
  shape) derives coarse events: session start/end (from `detect_pid`), idle
  detection (no records for N min), periodic "alive/doing <last context>" beats
  (`kind=active|note`).
- **Tier B — LLM informant:** a pi extension/prompt convention teaching the
  agent to emit structured events at action boundaries — "started task X"
  (progress), "finished milestone Y" (milestone), "blocked on Z" (poke),
  "check PR by 5pm" (reminder). A tiny `piso-informant` helper POSTs sanitized
  events; the agent supplies the text, the helper enforces shape/sanitization.
  Ships in the worker image (entrypoint); semantic content lives in pi's
  system prompt / memory.

### 4. Dashboard board tab (new main screen)

- New `#/board` tab, **default** boot tab (currently `log`; keep `log`
  reachable).
- **Render:** one horizontal **track per project slug**; items on a timeline
  by `ts`; **color-coded by kind** (progress=blue, milestone=green, reminder=
  amber, poke=red, active=pulsing); a live worker shows an "ongoing" item
  (latest informant `active`/`progress` + `WorkerCtx` project/branch/commit).
- Target-scoped pokes/reminders render on the target's track; source-defaulted
  (empty target) on the source slug's track.
- Data: control-plane `GET /api/v1/activities` + SSE `activity` events
  (reuse `routeURL`-style helpers, routes-tab poll/SSE patterns).
- Track grouping by slug survives worker restarts; a track dims / shows
  "idle Nm" past the grace window (reuse routes' `lastSeenMs` staleness).

### 5. Monitor worker (feed → LLM → pokes)

- Lifecycle: the gateway-compose sibling (Step 0). Same hardened image, no
  project mount, own agent volume.
- It runs pi with a "project manager" prompt: poll `GET /api/v1/worker/activities`
  (+ SSE), decide pokes/reminders from declared blockers + inactivity, `POST`
  them back as `kind=poke|reminder` with `target_slug` set. Internet toggle
  default off; scoped model key (Step 2) on when needed.
- **Delivery stays host-side:** the monitor only *writes* pokes; a host actor
  (`piso monitor` CLI/daemon, sync-daemon pattern) reads them and fires the
  real notification. Deferred (see Q3).

## Status

Steps 1–6 are IMPLEMENTED (SQLite activity store + APIs, Secret.Workers
scoping, monitor-in-compose, Tier A/B informants with idle-gating + monitor
Tier-A suppression + informant-prompt injection, board tab).

Step 7 (monitor LLM project-manager) is IMPLEMENTED:
- `piso secrets add-monitor <host> <value>` provisions the scoped model key
  (placeholder piso_monitor_…, Workers:[monitor], one command). CLI
  `secrets add` also accepts `--workers a,b`.
- `worker/MONITOR.md` — the PM prompt (read the scrubbed feed via worker API,
  decide pokes conservatively, emit via piso-informant; identity/limits).
- `worker/monitor-loop.sh` → installed as piso-monitor-loop: the wake loop
  (default 10 min, PISO_MONITOR_WAKE_SECS) that asks pi (single-shot -p) to
  run its routine per MONITOR.md, then sleeps. Default cadence in the loop.
- Role wiring: entrypoint suppresses Tier A for monitor; settings.prompts now
  includes BOTH /opt/piso/INFORMANT.md and /opt/piso/MONITOR.md (shared
  profile; the monitor's pi also loads the PM frame via its env + the loop's
  explicit -p prompt).
- Compose: monitor service has `command: piso-monitor-loop` + mounts
  workers/monitor/placeholders.env (its scoped key). piso up / rebuildGateway
  call EnsureWorkerPlaceholdersEnv("monitor").

Step 8 (host poke delivery daemon) remains DEFERRED; board is the v1 surface.

Monitor fix: the wake loop now `cd`s to a PERSISTENT workdir inside the agent
volume (/root/.pi/agent/monitor-work, falling back to /tmp). The monitor has
no project mount + read-only rootfs, so pi's default cwd (/workspace) made the
background-tasks extension fail with ENOENT on /workspace/.pi; the workdir
fixes extension state + keeps pi's project identity/session across restarts. Step 6 added: `worker/informant-help.sh` (installed as
`piso-informant`, on PATH for the agent) + `worker/INFORMANT.md` (the pi
prompt/memory convention: when to emit progress/milestone/reminder/poke/note
and how to target another project's slug). It is NOT a background watcher —
the agent calls it. Shipped in Dockerfile + hash + staging lists.

Step 3: monitor is a sibling service in compose/gateway.yaml (image piso-worker,
container piso-worker-monitor, slug "monitor", own volume piso-agent-monitor,
no project mount, internal vpc). `cmdUp` and `rebuildGateway` now build the
shared worker image BEFORE the gateway so the monitor's image exists at
gateway-compose time; `piso down`/`update` leave it running (they only touch
piso-<slug>). Gateway Dockerfile + Makefile ship go.sum; gateway build runs
`go mod download` in its own layer for the sqlite dep.

Step 4: worker/activity-watch.sh (Tier A informant: session start/end via
pi_present, alive beats every 120s with project context, posted via
POST /api/v1/worker/activity). Shipped in Dockerfile + entrypoint + hash list
+ staging; tests updated.

Two decisions confirmed during implementation:
- `Secret.Workers` matches the worker **slug** (workerIdentityFn resolves IP→
  slug), not the container name. A monitor key is scoped as
  `Workers: ["monitor"]`. `*`/empty = all.
- `go mod tidy` is now part of `make build` so the host resolves
  modernc.org/sqlite (v1.55.0) + go.sum on first build. No Go in container.

## Files to modify

- **go.mod** — add `modernc.org/sqlite` (+ pure-Go transitive deps).
- **Makefile** — `go mod tidy` before `go build` in the build target.
- **NEW `gateway/internal/store/activity.go`** — SQLite open/init/migrate,
  `InsertActivity`, `QueryActivities`, `DeleteOwnActivity`, `SubActivities`/
  `broadcastActivity`, `Close`.
- **MOD `gateway/internal/store/store.go`** — construct the activity DB at
  boot; store a handle (no `State.Activities`, no version bump); `Close`.
- **MOD `gateway/cmd/gateway/main.go`** — `-activities` flag
  (`PISO_ACTIVITIES_FILE`, default `.piso/activities.db`) passed to `New`.
- **MOD `compose/gateway.yaml`** — `PISO_ACTIVITIES_FILE: /data/activities.db`.
- **MOD `gateway/internal/server/worker.go`** (:8083) — `POST /activity`,
  `GET /activities`, `POST /activity/revoke`; validation + sanitize +
  identity check; `atoiDefault` + `strconv`.
- **MOD `gateway/internal/server/server.go`** — control-plane
  `GET /api/v1/activities`; SSE `activity` events in `stream()`.
- **MOD `gateway/internal/policy/policy.go`** — `Input.Worker`; enforce
  `Secret.Workers` in `substituteAll` via `secretAllowedForWorker`.
- **MOD `gateway/internal/proxy/proxy.go`** — fill `Input.Worker`
  (`h.workerID(req)`).
- **MOD `gateway/internal/server/ui/index.html`** — board tab (tracks,
  timeline, color code, live-active, inactivity dim), `#/board` default.
- **NEW `worker/activity-watch.sh`** — Tier A informant.
- **MOD `worker/Dockerfile`**, **`worker/entrypoint.sh`**, **`workerhash.Files`**,
  **`pisoconfig.CopyWorkerSkeleton`** — ship `activity-watch.sh` +
  `piso-informant` helper.
- **NEW `worker/informant-help.sh`** — Tier B helper.
- **NEW `compose/monitor.yaml`** or extend `compose/gateway.yaml` — the monitor
  sibling service.
- **NEW `plans/` note** — pi system-prompt/memory convention for Tier B.
- **MOD `Makefile`** — monitor is created by the same `piso up`/`setup` path
  (it lives in the gateway compose); ensure `docker compose up` includes it.

## Reuse

- `worker/context-watch.sh` — in-worker watcher skeleton (loop, curl POST to
  `$GATEWAY_URL`, env guards, sanitization, dedup); `activity-watch` is a thin
  specialization. `detect_pid` / /proc scanning already there.
- `gateway/internal/server/worker.go` — the :8083 trust boundary + validation/
  sanitize pattern from `handleWorkerPostContext`.
- `gateway/internal/store/store.go` — `broadcast`/`Sub` channel pattern →
  `SubActivities`; `ScanLog` replay idea if we ever want tail-backfill (not
  needed with SQLite).
- `gateway/internal/policy/policy.go` — `lookupRule`/`substituteAll`, the one
  place to add the `Workers` check.
- `gateway/internal/server/ui/index.html` — routes-tab patterns (poll, SSE,
  hash routing just added); `routeURL` link helpers.
- `SecretRec.Workers` field + dashboard Secrets form — scoping surfaces exist;
  only enforcement missing.
- `piso sync` daemon pattern — future host poke delivery (deferred).

## Steps

- [ ] **1. Activity store (SQLite) + worker API** — go.mod dep; `activity.go`
      schema/CRUD; `POST/GET/revoke` on :8083; control-plane `GET
      /api/v1/activities`; SSE `activity` events. Tests: insert/query/delete,
      sanitize, identity check, SSE.
- [ ] **2. Enforce `Secret.Workers`** — origin worker into policy input;
      substitution refuses secrets whose `Workers` excludes the origin. Tests:
      scoped substitutes for allowed worker, blocks others; empty/* unchanged.
- [ ] **3. Monitor lifecycle in gateway compose** — `monitor` sibling service
      (image, env, volume, networks), created with the gateway.
- [ ] **4. Informant Tier A** — `worker/activity-watch.sh`, shipped in image +
      entrypoint + hash list.
- [ ] **5. Dashboard board tab** — `#/board` default; tracks, timeline,
      color-coded items, live-active marker, inactivity dim; control-plane feed
      + SSE.
- [ ] **6. Informant Tier B** — `piso-informant` helper + documented pi
      prompt/memory convention; entrypoint ships it.
- [ ] **7. Monitor worker LLM** — scoped secret (Step 2), project-manager
      prompt (feed → decide → POST pokes), internet default off.
- [ ] **8. (deferred) Host poke delivery** — `piso monitor` CLI/daemon (sync-
      daemon pattern).

## Verification

- `go vet ./... && go test ./...` + `sudo make install` on the host; first
  `go mod tidy` fetches/vendors modernc.org/sqlite (pure-Go, no CGO; worker
  image build does not compile Go, so no Docker impact).
- **Step 1:** from a worker, `POST` an activity → appears in
  `GET /api/v1/activities` (control plane) and the worker feed; SSE emits
  `activity`; restart gateway → row survives (SQLite, no state.json bump).
- **Step 2:** `piso secrets add ... --workers piso-worker-monitor`; monitor
  substitutes its key; a normal worker sending the same placeholder is blocked
  (no-secret-rule) and retryable.
- **Step 3:** `docker compose -f compose/gateway.yaml ps` shows gateway +
  monitor; `piso up` starts both; `piso down` stops the worker but NOT the
  monitor (it's machine-level); reboot → monitor returns with the gateway.
- **Step 4:** `docker exec` the worker → session start/active/idle rows appear;
  sanitization strips control chars.
- **Step 5:** open `http://piso.local` → `#/board`; two projects → two tracks;
  live worker shows ongoing item; poke renders red on its target track; idle
  track dims after grace.
- **Step 7:** monitor worker, internet off by default; with scoped key on, it
  polls the feed and writes pokes visible on the board; a normal worker cannot
  read pokes targeting another project unless it's the target (feed is
  identity-scoped to own rows for non-monitors).

## Resolved decisions (was: Open questions)

1. **Poke targeting → `TargetSlug` field.** Pokes/reminders carry
   `target_slug`; the board renders them on that project's track. Empty →
   source slug's own track. No text parsing.
2. **Monitor identity → NO `IsMonitor`, same worker semantics.** The monitor is
   a normally-sandboxed worker whose slug `monitor` only names it; the API
   contract is uniform: any registered worker may read the feed and write its
   own rows. The host defines the monitor via the gateway compose (that is the
   designation); no code path special-cases the slug.
3. **Poke delivery → deferred to step 8.** The board is the v1 surface; the
   host notification daemon comes after the board proves useful.
4. **Activity lifetime → SQLite.** Full query surface, no ring caps, durable
   pokes/reminders; no state.json growth. Ring-buffer semantics replaced by
   bounded queries (board shows a rolling window; SQL `ORDER BY ts DESC LIMIT
   n`).