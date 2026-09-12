# Plan: fleet aggregator — relay worker activity to a central authenticated PM board

## Context

Today every piso gateway is a **silo**. Worker activity travels

```
worker pi → piso-informant / activity-watch.sh → POST /api/v1/worker/activity  (gateway worker-api :8083, VPC-only)
gateway   → identity check (IP↔registry) + sanitizeLabel
         → host-local SQLite activities.db (store/activity.go) → InsertActivity → SSE
         → host dashboard ui/index.html via the control plane (host-trust only)
```

and stays in that gateway's `.piso/activities.db`. The board renders only that
host's projects.

The activity model is already the right wire unit — `store.Activity`
(`id, worker, slug, kind, targetSlug?, text, ts`) — but every identity field is
**unique only per-host**: `worker` is a container name, `slug` derives from the
host's project dir name, `ts` is gateway wall-clock. Bridging to a combined
board therefore needs three things that don't exist today:

1. **A fleet dimension** — events must be attributed to a *site* (which gateway).
2. **Composite project identity** — `(gateway, slug)`, since two hosts can both
   have a `backend`.
3. **Real authentication** — the local control plane is gated only by network
   posture (`denyControlPeer` rejects the worker VPC; a host browser is trusted
   by being on the host). A shared board on a LAN/internet needs actual auth on
   the aggregation endpoint and on the UI.

End state (optional feature, zero behavior change when unconfigured): a gateway
can relay its sanitized activity stream to a central **authenticated
aggregator** — a second, slim piso binary serving the same board UI over a
*combined* feed — rendering every project across every enrolled gateway as a
timeline track.

## Decisions

| # | Question | Proposal | Status |
|---|----------|----------|--------|
| D1 | Aggregator shape | **Separate `aggregator` binary** reusing gateway store/routes/UI; gateways get an optional relay sink. One binary with a `--mode aggregator` flag is the fallback if packaging a second binary is painful. | open before step 0 |
| D2 | Transport direction | **Push**: gateway dials the aggregator over HTTPS (mirrors piso's outbound-only posture; hosts have no public ingress). A pull migration path (aggregator polls `worker/activities` like the monitor does) exists but is not the design. | proposed |
| D3 | Project identity | Ship `(gateway, slug)` composite as the track key; add an operator mapping table later if merging same-project lanes across hosts is wanted. | proposed |
| D4 | Dashboard auth | MWE: static per-fleet token (+ optional 2FA) on the aggregator's control port. OIDC adapter later — this is the first user-auth surface in piso and should be a thin middleware, not baked into routes. | proposed |

## Approach

### 1. Relay wire contract

Gateway → aggregator, one POST per event, HTTPS:

```
POST {agg}/api/v1/ingest
Authorization: Bearer <fleet token>            # per-gateway credential, gateway-side only
{
  "gateway":   "myhost",                       # site id from gateway flag, NOT worker-controlled
  "seq":       12345,                          # monotonic per gateway; aggregator acks it
  "op":        "put" | "revoke",
  "activity":  { id, worker, slug, kind, targetSlug?, text, ts }   # the stored, sanitized record
}
```

- **Idempotent**: aggregator dedupes on `(gateway, id)`; reconnect backfill
  replays from the last acked `seq` (`QueryActivities`-style, `since` filter).
- **Revocations**: `DeleteOwnActivity` today doesn't broadcast at all (the
  local board's 10s poll just makes rows vanish). The relay needs to carry
  revokes or the combined board diverges. Add an `activityRevoked` broadcast
  channel in the store (small; also lets local SSE clear rows promptly), and
  the relay forwards it as `op: revoke`.
- **No new trust in workers**: the relay reads *stored, sanitized* records from
  the gateway's DB — it never re-parses worker POST bodies, and the fleet token
  lives in gateway config (flag / host secrets file), never in `placeholders.env`
  or any worker-visible env (verify against `SecretVisibleToWorker`).

### 2. Aggregator store — schema v2

New fleet DB (fresh file, e.g. `PISO_AGG_ACTIVITIES_FILE`), reusing the
`activitySchema` shape with additions:

```sql
CREATE TABLE activities (
    id           TEXT,   -- source activity id
    gateway      TEXT NOT NULL,
    worker       TEXT NOT NULL,
    slug         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    target_slug  TEXT,
    text         TEXT NOT NULL,
    ts           INTEGER NOT NULL,          -- source gateway wall-clock (timeline x-axis)
    seq          INTEGER NOT NULL,         -- source sequence, for backfill ack
    receive_ts   INTEGER NOT NULL          -- aggregator wall-clock (stability order)
);
CREATE UNIQUE INDEX ux_agg_dedupe ON activities(gateway, id);
CREATE INDEX ix_agg_seq     ON activities(gateway, seq);
CREATE INDEX ix_agg_receive ON activities(receive_ts);
```

- Local gateway DBs stay untouched (backward compatible, no migration).
- Ordering: `ts` remains the board's timeline key, but cross-host clock skew
  means the aggregator orders ties by `receive_ts` (stable), not by `ts`.

### 3. Relay sink (gateway side)

New `gateway/internal/relay/relay.go`:

- On start: tell the aggregator the last acked `seq` (`GET /api/v1/gateways/{id}/ack` or a handshake in the ingest response) and **backfill** the gap from
  the local DB.
- Idle: subscribe `SubActivities()` (and the new revoke channel) and POST each
  event; advance the acked `seq` on 2xx.
- **Spool on failure**: the aggregator being down must not drop events — reuse
  the spool/drain pattern already proven in `worker/activity-watch.sh`
  (`SPOOL`/`SPOOL.drain`, bounded, oldest-drop) and port it to Go inside the
  relay, keyed per gateway `seq`.
- **Wiring**: `gateway/cmd/gateway/main.go` gains flags
  `--aggregator URL --fleet-token <path-or-value> --site-id NAME`;
  when `--aggregator` is unset the relay is a no-op and behavior is bit-identical
  to today.

### 4. Aggregator binary

New `gateway/cmd/aggregator/main.go`. It reuses the gateway's store, Activity
type, sanitizer, mux style, and embedded UI, but **serves only a read-only
activity slice plus ingest** — no proxy, CA, ingress, worker-api, secrets, rules,
or retry routes (smallest possible internet-facing surface):

- `POST /api/v1/ingest` — check `Authorization` (constant-time compare),
  validate kind via `ValidActivityKind`, re-`sanitizeLabel` every text field
  (defense in depth even though the source already sanitized), dedupe on
  `(gateway, id)`, insert, broadcast to SSE.
- `GET /api/v1/activities` — combined feed with added `gateway`, `seq`,
  `receive_ts` query filters; default cap raised vs the local 2000 (retention
  + per-gateway rate limit so one noisy worker can't drown the fleet).
- `GET /api/v1/gateways` — enrolled sites + last-seen (from `receive_ts` of the
  newest row per gateway, plus `active` heartbeats), so the board can dim a
  dead host.
- `GET /` + SSE — the embedded dashboard UI, driven by the same select-loop as
  `server.go stream()`.
- Bind exactly one web port; auth middleware (D4) sits in front of `/api/`.

### 5. Combined board (UI)

`ui/index.html` changes are additive:

- `loadBoard()` already calls `/api/v1/activities` + `/api/v1/workers`; the
  aggregator versions the endpoints and adds `gateways`.
- Track key becomes `<gateway>/<slug>` (composite) when more than one gateway
  is present; a filter pill row lets you scope `?gateway=…`.
- Dead-gateway dimming from `/api/v1/gateways` last-seen.
- Local single-host boards keep rendering exactly as today (gateway passes the
  composite key through unchanged when alone).

## Files to modify

```
gateway/internal/store/activity.go     # +activityRevoked broadcast; keep local schema
gateway/internal/store/agg.go (new)    # aggregator DB: schema v2, ingest insert, dedupe, query
gateway/internal/relay/relay.go (new)  # sink: subscribe, backfill, spool, ack
gateway/cmd/gateway/main.go            # --aggregator/--fleet-token/--site-id flags, relay start
gateway/cmd/aggregator/main.go (new)   # slim binary: ingest + combined API + UI + auth
gateway/internal/server/ui/index.html  # gateway filter, composite keys, dead-host dim
gateway/internal/server/auth.go (new)  # token/OIDC middleware for the aggregator mux
compose/…                              # optional aggregator service (image: piso-gateway, aggregator mode)
```

## Reuse

- **Spool/drain**: pattern from `worker/activity-watch.sh` (SPOOL, drain file,
  bounded, oldest-drop) — port to Go in the relay.
- **Backfill**: `Store.QueryActivities` + `sinceMs` filter already covers
  resume-from-ack.
- **SSE**: the select-loop in `server.go stream()` (`SubActivities`, ticker,
  flusher) drives the aggregator board unchanged.
- **Store/sanitize**: `store.New` (activities parts), `store.Activity`,
  `ValidActivityKind`, `sanitizeLabel`, `randID`.
- **UI**: the board in `ui/index.html` (tracks, runs, kind colors, filters)
  renders the combined feed with minimal additive changes.
- **Auth precedent**: `denyControlPeer` is the model for "wrap the mux in a
  gate"; the aggregator replaces network posture with a credential check.

## Steps

1. **Decide D1–D4** (shape, transport, identity, auth) — this doc's proposals
   are the default.
2. **Aggregator store**: schema v2 + `store/agg.go` with unit tests (dedupe on
   `(gateway,id)`, revoke removes, query filters incl. `gateway`/`since`/`seq`).
3. **Revoke broadcast** in `activity.go` (local SSE clears rows; relay gets a
   channel).
4. **Relay sink** `relay.go` + `main.go` flags; spool/drain ported to Go;
   backfill-on-reconnect; unit tests with a stub aggregator (HTTP stub that
   accepts/acks/rejects).
5. **Aggregator binary**: `cmd/aggregator/main.go` with `/api/v1/ingest`,
   combined `/api/v1/activities`, `/api/v1/gateways`, SSE, embedded UI.
6. **Auth middleware** (D4): token check first, wired before `routes()`.
7. **Board additions** in `ui/index.html`: gateway filter, composite keys,
   dead-host dim; verify local-mode rendering unchanged.
8. **E2E on a live stack**: two gateways → one aggregator; kill/restart a
   gateway mid-stream (no gaps thanks to spool+backfill); revoke an activity on
   one host and see it clear on the combined board; bad token → 401.

## Verification

- `go test ./gateway/internal/store` — dedupe/revoke/backfill tests pass;
  existing activity tests untouched (schema unchanged locally).
- `go test ./gateway/internal/relay` — spool survives aggregator outage;
  reconnect replays exactly the unacked range; non-2xx re-queues.
- `go test ./gateway/internal/server` — full suite still green (relay inert
  without `--aggregator`).
- Manual: two-host combined board renders composite tracks; gateway filter
  works; dead host dims; `revoke` clears across hosts; unauthorized ingest
  rejected; local single-host board byte-identical in behavior.
- `/tmp` is noexec in this sandbox — run Go tests with `TMPDIR` on the agent
  volume, per `plans/plan-activity-resilience.md` Verification.