# Plan: richer request-log labels (repo / project / path, branch, commit, model)

## Context

The dashboard request log currently labels each row with the worker slug only.
The user wants more context per row: git repo / project or folder path, git
branch, git commit, and the pi model name.

These facts only exist **inside the worker** (project mount, git state, live pi
session) and change over time (agent checks out branches, switches models
mid-session). So this is a new data plane — a worker-side watcher pushing
context to the gateway, snapshotting it onto each log row at record time — not
just a dashboard tweak.

Semantics: labels describe the request's **origin worker at that moment**.
They are advisory metadata, never security decisions.

## Approach

### Data flow (mirrors the proven planning-watch pattern)

```
worker: context-watch.sh (in-container, ~1s poll, no docker on host)
   ├─ detect live pi pid(s) via /proc cmdline scan   (reuse sessions.go regexes)
   ├─ folder  = readlink /proc/<pid>/cwd             (fallback /workspace)
   ├─ project = basename(git -C <folder> rev-parse --show-toplevel)  (git -c safe.directory=*)
   │            fallback: basename(folder) when not a git repo
   ├─ branch  = git -C <folder> branch --show-current            ("" when detached)
   ├─ commit  = git -C <folder> rev-parse --short HEAD           ("" outside a repo)
   ├─ model   = last "model_change" in newest session jsonl → "provider/modelId"
   │            fallback: PI_PROVIDER/PI_MODEL from /proc/<pid>/environ
   └─ POST (only when the tuple changed) →
      WorkerHandler POST /api/v1/worker/context  (same :8083 trust boundary as planning)
```

### Snapshot at record time (chosen over display-time join)

`AppendLog` looks up the worker's current context by `rec.Slug` and fills
`Record.Project/Branch/Commit/Model`. This is the single choke point — every
path that logs (SNI intercept internal block, internet-kill-switch block, block /
substitute / allow, 502, replay) gets labels for free, history stays stable
(branch at the time of the request), and filtering/JSONL/SSE all work on the
persisted shape. Cost: labels lag reality by ≤~1–2 s (poll interval) around a
`git checkout` — irrelevant for log rows.

Backward compatible: `Record` gets four new `omitempty` string fields; the
in-memory ring (source of truth for `/api/v1/requests`) is populated at
runtime, and the JSONL log is append-only (never re-read). Old rows decode
fine and render without labels.

### Gateway state

New `Contexts []WorkerCtx` map (by slug) in `State`, persisted in state.json —
labels survive gateway restarts; the watcher re-posts on change anyway.
Bump `CurrentStateVersion` 2 → 3 with a v2→v3 migration that normalizes a nil
`contexts` slice (house pattern from v1→v2). Stale-ctx property: last-seen
context persists if the worker dies — correct for log snapshots.

## Files to modify

- **NEW `worker/context-watch.sh`** — the poller/reporter (bash, same style as
  `planning-watch.sh`: loop + curl POST on change; sanitize every label —
  strip control/whitespace, cap length ~120 — since values render in HTML).
- **MOD `worker/Dockerfile`** — `COPY context-watch.sh` + `chmod +x`.
- **MOD `worker/entrypoint.sh`** — launch it `nohup` when
  `GATEWAY_URL` + `PISO_WORKER_NAME` + `PISO_WORKER_SLUG` set (same guard as
  the planning-watch launch).
- **MOD `cli/internal/workerhash/workerhash.go`** — add `"context-watch.sh"` to
  the `Files` list so the shared image rebuilds when it changes.
- **MOD `gateway/internal/store/store.go`** —
  - `type WorkerCtx` (`worker`, `slug`, `folder`, `project`, `branch`, `commit`,
    `model`, `ts`; all labels `omitempty` except slug).
  - `State.Contexts` field + `UpsertWorkerCtx` (upsert by slug) +
    `WorkerCtxBySlug`.
  - `Record` + `Project/Branch/Commit/Model string json:"…,omitempty"`.
  - `AppendLog`: enrich from context by `rec.Slug` when the label fields are
    empty (never overwrite a label an explicit call site set).
- **MOD `gateway/internal/store/migrate.go`** — v2→v3 migration for
  `Contexts`.
- **MOD `gateway/internal/server/worker.go`** (WorkerHandler, :8083) —
  `POST /api/v1/worker/context` handler: validate worker name + slug
  (`workerNameRe` / `slugFromWorker`), sanitize fields, `UpsertWorkerCtx`,
  return `{ok:true}`. No model.go change needed — `store.Record` is what
  `/api/v1/requests` serializes.
- **MOD `gateway/internal/server/ui/index.html`** —
  - `logRowCells`: second muted detail line with the non-empty labels, e.g.
    `repo <project> · <branch> · <commit>` and `model <provider/model>`;
    folder shown instead when not a git repo (project = folder basename,
    branch/commit empty).
  - `logMatches`: also match project / branch / commit / model / folder.
  - Workers tab: show each worker's live context row (slug + project + model +
    ts) — small, reuses `/api/v1/workers` payload extended with context.

## Reuse

- `worker/planning-watch.sh` — watcher skeleton (poll loop, curl POST to
  `$GATEWAY_URL`, notify-on-change, env guards).
- `cli/cmd/piso/sessions.go` — the python3 `/proc` cmdline scan for real pi
  processes (regexes `pi-coding-agent` / `(^|\s)pi(\s|$)`, exclude
  `piso-entrypoint|piso-planning-watch|docker-init|sleep infinity`); the image
  has no pgrep. Copy into context-watch.sh.
- `gateway/internal/server/ingress.go` — `workerNameRe`, `slugFromWorker`.
- `gateway/internal/store/store.go` — `UpsertWorker` shape for
  `UpsertWorkerCtx`; `WorkerBySlug` pattern.
- Session file format: newest `*.jsonl` under
  `/root/.pi/agent/sessions/--workspace--/` (pi sanitizes `/workspace` →
  `--workspace--`, confirmed in main.go); first line `model_change
  {provider, modelId}`. Tail last ~64 KiB only (sessions reach MBs).
- `sudo make install` on the host is the only Go build path (no toolchain in
  the worker — known constraint).

## Steps

- [ ] **1. Worker watcher** — write `worker/context-watch.sh`; verify manually
      in a container (`docker exec`): correct branch/commit/model detection,
      folder fallback outside git, sanitization, dedup-on-unchanged.
- [ ] **2. Build plumbing** — Dockerfile COPY, entrypoint launch, add file to
      `workerhash.Files`. (Rebuild is automatic: the new file changes the
      context hash.)
- [ ] **3. Gateway store** — `WorkerCtx` type + `State.Contexts` + version 3
      migration + `UpsertWorkerCtx` / `WorkerCtxBySlug`; extend `Record`;
      enrich in `AppendLog`.
- [ ] **4. Handler** — `POST /api/v1/worker/context` on WorkerHandler with
      validation/sanitization.
- [ ] **5. Tests** — store_test: ctx upsert + append-log enrichment (labels
      applied only when row has no explicit label); worker_test/workers_test:
      handler validation + round-trip.
- [ ] **6. Dashboard** — label line in `logRowCells`, extended `logMatches`,
      Workers-tab context.
- [ ] **7. End-to-end** — rebuild, `piso up` in a git repo, curl from the
      worker, confirm labels + JSONL + filter; repeat outside a repo.

## Verification

- `go vet ./... && go test ./...` on the host, then `sudo make install`
  (rebuilds gateway + re-stages worker image), `piso setup --rebuild`.
- `piso up` in this repo → worker runs context-watch; `docker exec …
  cat /tmp/piso-context-watch.log` shows detection + 200s.
- `GET /api/v1/requests` rows carry `project/branch/commit/model`;
  `~/.piso/requests.jsonl` rows carry the same fields.
- Dashboard: row shows repo/branch/commit/model; filter by branch or model
  works; non-git folder falls back to folder basename with empty branch/commit.
- Existing `~/.piso/state.json` (v2) migrates cleanly (v3) — `make gw-run`
  against a pre-existing state dir.