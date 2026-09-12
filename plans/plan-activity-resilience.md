# Activity reporting — resilience plan

## Context

Activity shown on the piso PM board is produced by a two-tier chain, and every
link can silently drop events:

- **Tier A** (`worker/activity-watch.sh`, launched fire-and-forget by
  `worker/entrypoint.sh`): posts `active` beats (≤1/min) and `note`
  session start/end to `http://gateway:8083/api/v1/worker/activity`.
- **Tier B** (`/usr/local/bin/piso-informant`, called by the agent): posts
  `progress|milestone|reminder|poke|waiting|note` to the same endpoint.
- **Gateway** (`gateway/internal/server/worker.go`): validates, enforces an
  identity check against the slug↔IP registry, stores into SQLite
  (`gateway/internal/store/activity.go`), broadcasts over SSE.

Failure points found during investigation:

1. **Stale-IP 403 (hard, silent failure).** The slug↔IP registry is populated
   only by the host CLI at `piso up` (`cli/cmd/piso/main.go:235
   registerWorkerWithGateway`). When a worker container is recreated (rollout,
   `docker compose up` outside `piso up`), it gets a new vpc IP; the registry
   keeps the old one, so **every** `activity`/`ports`/`context` POST returns
   403 "identity mismatch". Nothing retries or self-heals; the informant just
   prints `informant 403 …` and the board goes quiet.
2. **Watchers are unsupervised.** `entrypoint.sh` nohups them; if one dies
   (crash, OOM) nothing restarts it. Missing env vars make them `exit 0`
   silently, and their logs go to `/tmp` (lost).
3. **No retry or buffering.** `curl --max-time 3`, single-shot. A gateway
   blip longer than the board's 3-minute merge window reads as downtime, and
   failed Tier B events are gone forever (Tier B failures are non-fatal by
   convention, so the agent never retries).
4. **Failures are invisible.** 403/400/500 bodies are terse; the board cannot
   distinguish "no work happened" from "the feed is dead".

Goal: make the reporting chain self-healing where the failure is mechanical
(identity, dead process, transient network), and visible where it cannot
self-heal (agent simply not emitting).

## Approach

### Phase 1 — Self-healing worker identity (kills the 403 class)

**Gateway: `POST /api/v1/worker/checkin` on the worker API** (`:8083`,
`WorkerHandler` in `gateway/internal/server/worker.go`). Same trust model as
`piso up`: source IPs on the vpc bridge cannot be spoofed, registration is
first-come.

Handler logic (body `{worker, slug}`, reusing existing validation):
1. Validate `worker`/`slug` with `workerNameRe` / `slugFromWorker` (same as
   `handleWorkerPostActivity`).
2. `ip := requestIP(r)`; look up `WorkerByIP(ip)`:
   - found, same name+slug → 200 (no-op; optionally refresh `UpdatedAt`).
   - found, different name/slug → **403** with detail (IP owned by someone
     else — strict, preserves anti-squatting).
   - not found → merge `ip` into the record for `in.Worker` via
     `WorkerByName` + `UpsertWorker` (append, don't replace existing IPs),
     return 200 with the authoritative record.
3. Idempotent; callable at every boot.

**Worker: checkin on startup.** In `worker/entrypoint.sh`, before launching
the watchers, POST the checkin (inline curl, `|| true`, non-fatal) when
`GATEWAY_URL`/`PISO_WORKER_NAME`/`PISO_WORKER_SLUG` are set. After a recreate,
the new IP is claimed and all subsequent watcher/informant posts pass the
existing identity check (which stays as-is: `reg.Name != in.Worker ||
reg.Slug != in.Slug → 403`).

### Phase 2 — Supervise and log the watchers

In `worker/entrypoint.sh`, replace the bare `nohup … &` launches with
self-respawning loops, e.g.:

```sh
respawn() { local bin=$1 log=$2; ( while :; do "$bin" >>"$log" 2>&1; echo "$(date -u +%FT%TZ) $bin exited $?, restarting" >>"$log"; sleep 3; done ) & }
```

- Logs under `/var/log/piso/` (mkdir on boot) instead of `/tmp` — survives
  restarts of the container, inspectable on the host.
- Missing-env failures: watchers already `exit 0`; change to a clear
  "piso-activity-watch: missing GATEWAY_URL/PISO_WORKER_NAME/PISO_WORKER_SLUG"
  logged to the persistent log file before exiting, so supervision logs show
  *why* nothing runs. (`piso-activity-watch.sh`, `piso-context-watch.sh`
  currently `exit 0`; the monitor loop already prints.)

### Phase 3 — Retry + spool (Tier A and Tier B)

Shared, simple spool: a JSONL file under the **agent volume**
(`/root/.pi/agent/spool/activity.jsonl`, survives container recreates),
flock-guarded, bounded (~200 lines / 512 KB, drop-oldest with a
`{"kind":"note","text":"spool overflow, dropped N events"}` marker).

- **`piso-informant`**: on a non-200/201 response (or curl failure), append the
  sanitized event line to the spool under `flock`, print `informant 000
  (spooled)`, exit 0 — the agent's "non-fatal, continue" contract is kept and
  nothing is lost. Add `curl --retry 1 --retry-connrefused --retry-delay 1`
  for the common transient case (still ≤ ~5s total).
- **`piso-activity-watch.sh`**: every loop pass, drain the spool: take
  `flock`, read lines, POST each to the same endpoint with the same
  `san()`/exit-code rules, remove on 200/201, keep on failure. This also
  becomes the retry buffer for the watcher's own failed beats and session
  notes — after a post failure, retry on the 5s loop instead of waiting a
  full 60s beat, so a short gateway blip stays inside the board's 3-minute
  merge window.
- Replays are idempotent-enough (in-flight duplicates are rare and harmless:
  they render as adjacent same-kind rows; recording a `clientTs` in the spool
  line and gateway-side is out of scope — note in code).

### Phase 4 — Make failures visible

- **Gateway 403/400 bodies carry detail** (`worker.go`): e.g.
  `{"error":"identity mismatch","detail":"registered piso-worker-a@10.0.0.7;
  request claims worker=piso-worker-b slug=b"}`. `piso-informant` already
  prints the response body, so the agent line becomes self-explanatory.
- **Board "feed stale" indicator** (`gateway/internal/server/ui/index.html`):
  on each track header, render a small "last active Xm ago" pill derived from
  the loaded activities feed (most recent `active` event for that slug),
  amber when older than ~10 minutes. Turns silent feed death into a visible
  amber flag without new endpoints — the board already pulls
  `/api/v1/activities`. (Phase 4b; optional — UI polish, no wiring risk.)

## Files to modify

| File | Change |
| --- | --- |
| `gateway/internal/server/worker.go` | Add `POST /api/v1/worker/checkin` on `WorkerHandler`; richer 403/400 bodies |
| `gateway/internal/server/worker_test.go` (new) | Checkin handler tests (mirror `ports_test.go` `doJSON` pattern) |
| `worker/entrypoint.sh` | Checkin-on-boot curl; `respawn()` wrapper + `/var/log/piso` logs |
| `worker/activity-watch.sh` | Spool drain per loop pass; tight retry on failure; log env-missing clearly |
| `/usr/local/bin/piso-informant` → `worker/informant.sh` (repo copy lives in `worker/`, shipped to the image) | curl retry + spool fallback on failure |
| `gateway/internal/server/ui/index.html` | "last active" pill (Phase 4b) |

## Reuse

- `workerNameRe`, `slugFromWorker`, `requestIP`, `writeJSON`, `sanitizeLabel` —
  `gateway/internal/server/worker.go`, `ingress.go`
- `Store.WorkerByIP`, `Store.WorkerByName`, `Store.UpsertWorker`,
  `WorkerRec.IPs` merge semantics — `gateway/internal/store/store.go`
  (note: `UpsertWorker` replaces the IP list keyed by name, so the handler
  must merge via `WorkerByName` first)
- Watcher `post()`/`san()` and `curl --max-time 3` conventions —
  `worker/activity-watch.sh`, `worker/context-watch.sh`
- Watcher launch block — `worker/entrypoint.sh` (lines launching
  planning/ports/activity/context watches)
- SSE/poll board refresh + `/api/v1/activities` — `ui/index.html`
  (`loadBoard`, `MERGE_MS`)

## Steps

- [x] **P1** `worker.go`: implement `handleWorkerCheckin`; register on
  `WorkerHandler`
- [x] **P1** merge-IP logic via `WorkerByName` + `UpsertWorker` (append, don't
  clobber); slug conflict → 403 with detail
- [x] **P1** `entrypoint.sh`: checkin-boot block (curl, `|| true`), placed
  before watcher launches
- [x] **P1** tests: checkin idempotency, new-IP claim, cross-name 403
- [x] **P2** `entrypoint.sh`: `respawn()` wrapper for all four watchers;
  log dir on the agent volume (see deviation note)
- [x] **P2** watchers: env-missing → log to stderr (flows into the respawn
  log), exit nonzero (3)
- [x] **P3** `informant`: `--retry 1` + spawn/append to spool under flock;
  exit 0 with `(spooled)` note when buffered
- [x] **P3** `activity-watch.sh`: drain spool each loop pass; failed
  beats/notes re-queued to spool; retry cadence tightens after failure
- [x] **P4** richer 403/400 bodies in `worker.go` (`identityMismatch` helper)
- [x] **P4b** board "last active" pill in `ui/index.html`

## Deviations from the plan (with reasons)

- **Log location: `/root/.pi/agent/logs` instead of `/var/log/piso`.** Both
  worker and monitor containers are `read_only: true` — `/var/log` is not
  writable anywhere. The agent volume is the single writable, persistent
  location in every container.
- **`active` beats are never spooled.** They are ephemeral alive-signals;
  replaying stale ones after an outage would mislead the board. Only durable
  kinds (notes + informant events) are buffered.
- **Spool drains at most 25 lines per loop pass** so a large backlog cannot
  stretch the 5s watcher loop latency unboundedly.

## Verification

Executed inside the hardened worker (no Go toolchain on PATH, but one is
vendored at `/workspace/.tools/go/bin/go` — needs writable caches since the
rootfs is read-only):

```bash
cd /workspace
export PATH=/workspace/.tools/go/bin:$PATH HOME=/root/.pi/agent \
       GOPATH=/workspace/.tools/gopath GOCACHE=/workspace/.tools/gocache \
       TMPDIR=<exec-capable tmp>   # /tmp is noexec here; use the agent volume
go test ./gateway/internal/server   # ALL new checkin tests PASS
```

- `go test ./...` compiles clean; the server package (with `worker_test.go`)
  passes every test except `TestWorkerActivityPostGetRevoke` — which fails
  **identically on the pristine tree** (verified via `git stash`), i.e.
  pre-existing in this environment. The remaining repo failures are
  environmental: `TestReconcileIdempotent` (read-only `/etc/hosts`),
  `TestWorkerInternetBlocked` (no MITM upstream in this container),
  `TestSetWorkerUnreachableHints` (untouched package).
- `bash -n` clean on all six modified shell scripts.
- Spool/drain logic round-trip tested in isolation: append → drain →
  post-accept clears; rejected lines re-queue intact; concurrent append dur-
ing drain is never clobbered; 200-line cap drops oldest, keeps newest.
- Informant exercised against a connection-refused stub (spools, exits 0,
  ~1s incl. retry) and a 201 HTTP stub (posts, escapes quotes/backslashes,
  does NOT spool).
- Board `bStaleInfo` unit-tested in node (6 cases: fresh/stale/edge cases).
- Manual E2E items (recreate worker w/o `piso up`, gateway outage + drain,
  kill watcher + respawn, amber pill rendering) remain to be run on a live
  piso stack by the human.