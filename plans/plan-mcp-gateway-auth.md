# Plan: gateway-brokered MCP OAuth — dashboard login, placeholder in the worker

## Context

Remote MCP servers (e.g. `https://mcp.ai-boost.io/mcp`) speak OAuth. Today
`pi-mcp-adapter` runs that dance **inside the worker**: DCR, browser redirect to
`localhost:<port>/callback`, tokens in the encrypted file store, then
`Authorization: Bearer <real token>`. That hits two walls this sandbox already
proved:

1. Callback is loopback in the container; the host browser never reaches it
   (manual paste of the redirect URL).
2. The gateway correctly **407 `credential-pattern`s** a real Bearer token.
   Placeholders (`Bearer piso_…`) already substitute; real tokens must never
   live in the worker (threat model asset #1).

Desired user flow (maximally automated):

1. User asks the worker LLM to set up an MCP server (URL).
2. An MCP login item appears on the gateway dashboard (chip + MCP tab).
3. User completes login **in the dashboard** (host browser, gateway callback).
4. Gateway stores the real tokens; worker only ever sees a `piso_…` placeholder.
5. Worker `mcp.json` uses `auth: "bearer"` + that placeholder.
6. Connect succeeds (retry of the captured request, no second ceremony).

Reorder vs the prompt: the **placeholder is written to `mcp.json` before
login** (step 5 happens as part of 1–2). Login fills the vault. First connect
before login is a retryable 407; after login the gateway auto-replays it
(existing block → fix → retry). The user should not have to “try connect
again” by hand.

## Approach

Gateway is the OAuth client and the vault. Worker is a bearer client with a
fake credential. Same substitution / capture / `placeholders.env` machinery as
Routstr keys.

### 1. Worker announces intent (LLM tool → worker API)

When the user asks to add an MCP server, the agent:

1. POSTs to the vpc-only worker API (`:8083`), same trust boundary as ports /
   planning:
   `POST /api/v1/worker/mcp` body `{worker, slug, name, url}`.
2. Gateway validates HTTPS URL (no credentials/fragments, not loopback), mints
   placeholder `piso_mcp_<slug>_<name>`, creates an **empty** vault secret
   (worker-scoped, `allowedHosts: [mcp-host]` only — never `*`), a substitution
   rule for that host, and a **pending MCP auth** row.
3. Agent writes `/root/.pi/agent/mcp.json` immediately:

```json
{
  "mcpServers": {
    "ai-boost": {
      "url": "https://mcp.ai-boost.io/mcp",
      "auth": "bearer",
      "bearerToken": "piso_mcp_piso_ai-boost"
    }
  }
}
```

`auth: "bearer"` is mandatory so `pi-mcp-adapter` does **not** start in-worker
OAuth on 401.

No encrypted-file store, no OS keyring, no `PI_MCP_ADAPTER_OAUTH_FILE_KEY` on
this path.

### 2. Dashboard: MCP tab + authorize chip

- **MCP tab**: per-worker rows (name, URL, placeholder, status
  `needs-auth` | `ok` | `expired` | `revoked`). Actions: Authorize, Re-auth,
  Revoke. Never display real tokens (same as Secrets).
- **Chip** (plan-review pattern, SSE): “Authorize ai-boost for <slug>”.
  This is the in-the-moment surface so the user is not hunting a tab while the
  agent is blocked.

Authorize starts RFC 8707 / MCP OAuth **on the gateway**:

- Discover `/.well-known/oauth-protected-resource/...` + AS metadata
- DCR as client “piso” (client id/secret stay in `state.json`)
- Authorize URL with `redirect_uri=https://mcp-oauth.piso.local/callback`
  (gateway ingress, not worker localhost)
- PKCE + `resource=` bound to the MCP URL
- Chip click opens the host browser (same class as plan review)

### 3. Callback lands on the gateway

`mcp-oauth.piso.local` is a control-plane/ingress name, not a worker route.
Callback handler:

- Validates `state` / PKCE
- POSTs the token endpoint **from the gateway** (egress, not via worker)
- Stores `access_token` / `refresh_token` on the secret
- Marks the MCP row `ok`
- Rewrites `workers/<slug>/placeholders.env` (placeholder already listed;
  value is still `piso_…` only)
- Auto-replays matching captured 407s (same as `POST /api/v1/secrets`)

Worker never sees the token response body.

### 4. Connect

Worker sends `Authorization: Bearer piso_mcp_…`. Scanner already treats
wrapped placeholders as non-secrets. Substitution runs. Upstream MCP gets the
real Bearer.

If the user connects **before** login: 407 with a new reason
`mcp-auth-required` (retryable, captured). Chip is already up. After step 3,
replay succeeds — that **is** step 6, automatic.

Refresh: on upstream 401 (or near expiry) the gateway refreshes with the
stored refresh token, rotates the secret, retries. Placeholder unchanged.

Revoke: drop tokens, row → `needs-auth`, next request 407 + chip again.

## Status model

New store types (sketch):

```
McpServerRec {
  ID, Worker, Slug, Name, URL, Placeholder, SecretID,
  Status: needs-auth | ok | expired | revoked,
  Resource, AuthURL?, ExpiresAt?
}
```

Secret is a normal `SecretRec` (no parallel vault). MCP tab is a view + OAuth
verbs on top of it. `CurrentStateVersion` bump + migration.

Pending auth is a small row (or reuse an ingress-like pending table keyed
`(worker, mcpName)`) so SSE/chip wiring matches planning.

## Files

- **MOD `gateway/internal/model/model.go`** — `McpServer` summary (no secrets);
  reason `mcp-auth-required`.
- **MOD `gateway/internal/store`** — `McpServerRec`, CRUD, pending auth,
  version bump.
- **MOD `gateway/internal/server/worker.go`** — `POST /api/v1/worker/mcp`
  (and GET list for the agent if useful).
- **MOD `gateway/internal/server`** — OAuth broker (discover, DCR, authorize,
  callback, refresh); host control routes for MCP tab.
- **MOD `gateway/internal/server/ingress.go` / hosts** — `mcp-oauth.piso.local`
  callback (control-plane, not worker). CLI hosts sync already reconciles
  labels; add this one.
- **MOD `gateway/internal/proxy`** — on 407, if placeholder belongs to an MCP
  row still `needs-auth`, reason `mcp-auth-required`; after token save, replay
  like secrets. Do **not** allow real Bearers; do **not** add a
  `generic-bearer` exception.
- **MOD `gateway/internal/server/ui/index.html`** — MCP tab + authorize chip
  (reuse planning chip/SSE).
- **MOD worker agent path** — no image change required for v1: the LLM writes
  `mcp.json` after the worker API returns the placeholder. Optional later:
  entrypoint/helper so attach always merges gateway-listed MCP servers.
- **MOD `docs/design.md`** — MCP OAuth is a vault secret minted by a host
  chip; workers are bearer clients with placeholders only.
- **TEST** — worker API validation; callback stores secret and never echoes
  it; substitution on `mcp.ai-boost.io`; 407 before auth; replay after;
  refresh rotates value not placeholder; real Bearer still 407
  `credential-pattern`.

## Reuse

- Planning chip / `SubIngress` SSE — chip + tab attention.
- `AddSecret` + `SyncRulesForSecret` + `WriteWorkerPlaceholdersEnv` — vault
  and env files.
- Secret create → auto-replay of matching captures.
- Scanner: `Bearer piso_…` is not `generic-bearer`.
- WorkerHandler `:8083` identity (`worker`, `slug`).

## Pitfalls

- **Do not leave `url`-only entries in `mcp.json`.** Adapter will OAuth
  in-worker. Install path must always set `auth: "bearer"`.
- **Do not rewrite `/token` responses toward the worker.** Broker on the
  gateway; tokens never enter the tunnel to the worker.
- **`allowedHosts` is the MCP origin only.** A stolen placeholder must not
  substitute onto some other host.
- Callback must be gateway-owned. Worker `localhost:36705` is a dead end.
- Empty secret must not substitute as `Bearer ` (blank). Treat as
  `mcp-auth-required` until the callback fills it.
- DCR client secret is also a real credential — gateway-only, same as tokens.
- Existing encrypted-file + `PI_MCP_ADAPTER_OAUTH_FILE_KEY` is a local
  workaround; this plan supersedes it for piso-managed MCP. Leave the keyring
  workaround in place for stock Pi outside this flow; do not use it here.

## Out of scope (v1)

- In-worker `pi-mcp-adapter` OAuth / encrypted-file as the piso path.
- Pattern exception for `generic-bearer`.
- Gateway writing `mcp.json` on the agent volume (no mount).
- Multi-resource tokens, non-OAuth MCP, stdio MCP.
- Auto-discovery of MCP URLs the user did not ask to add.

## Verification

1. From a worker: announce `https://mcp.ai-boost.io/mcp` → dashboard chip +
   MCP tab row `needs-auth`; `mcp.json` contains `auth: "bearer"` and
   `piso_mcp_…` only.
2. Connect before login → 407 `mcp-auth-required`, request captured.
3. Complete login on the dashboard (host browser, `mcp-oauth.piso.local`).
4. Capture auto-replays; `mcp({ connect: "ai-boost" })` succeeds; tools list
   non-empty.
5. Worker disk/env/`mcp.json` never contain the access token (grep the
   placeholder only).
6. A raw `Authorization: Bearer sk-…` to that host still 407
   `credential-pattern`.
