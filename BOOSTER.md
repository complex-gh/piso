# Sandboxed coding agent with a credential gateway

A pattern for letting an untrusted coding agent work in a real project
without ever holding real credentials or reaching the network except
through an inspectable hop.

This is a **contract**, not a recipe. Any isolation runtime, any agent
CLI, any language for the control plane can satisfy it. If a section is
only true of one stack, it does not belong here.

## When to use

Use this when you want an LLM to edit a repo, run tests, and talk to
APIs, but you do not want the model (or anything it runs) to see API
keys, cloud tokens, or the rest of the host.

Do not use this as a general “secure the laptop” design. The host user
and the control plane are trusted. The agent is not.

## Threat model

**Attacker.** The agent process, any code it executes, and any
prompt-injected content it reads (repos, docs, build output).

**Assets, in order.**

1. Real credentials — must never exist in the worker (disk, env, memory).
2. Host filesystem — only the project mount is visible.
3. Host network — the worker has no path to the internet, LAN, or
   instance metadata except through the gateway.

**Trusted.** The host user, the gateway, and the isolation boundary
around the worker.

**Out of scope.** Exfiltration of data the agent is allowed to read (a
worker with a mount and any egress can upload source — that is
inherent). A compromised host. Denial of service.

The agent is the *attacker*, not the trust root. Everything it can do is
bounded by the worker plus the gateway.

## Topology

```
host (human, browser, CLI)
        │
        ▼
   GATEWAY          control plane + policy + vault + ingress
        │           sole egress; TLS inspect; substitute or block
        ▼
   WORKER           untrusted agent + project
                    one mount, no host net, no nested isolation
```

Three plug points sit *around* the gateway. They are independent.

| Driver | Job |
|---|---|
| **Runtime** | Create/destroy the box, mounts, exec, inspect identity |
| **Agent** | Interactive session, headless prompt, profile without secrets |
| **Monitor** | Optional: idle/blocked nudges from an activity feed |

The gateway does not know which process made a request. Do not collapse
these three into one “backend.”

## Worker contract

The worker is whatever the runtime spawned. It must speak this, or you
are building a different product.

- **Egress.** All outbound HTTP(S) goes through the gateway, with the
  gateway CA trusted. If a client ignores proxy env or the system store,
  it is broken, not optional.
- **Filesystem.** One project directory, read-write, shared with the
  host. One persistent volume for agent state (sessions, tools). Nothing
  else from the host — no `~/.ssh`, no credential files, no docker
  socket.
- **Secrets.** Only opaque placeholders (`prefix_…`). Real values never
  appear in env, files, or logs inside the worker.
- **Identity.** The gateway can map a request to a worker (source
  address, not a name the agent claims).
- **Capabilities.** On start, the agent fetches live boundaries from
  the gateway: internet on/off, which placeholders this worker may see.
  A failed fetch means *blocked*, not open.
- **Ports.** A process that binds a port is publishable through gateway
  ingress. The worker does not punch holes in the host firewall.
- **Out of bounds.** Steps the box cannot do (privileged git, nested
  containers, raw SSH) are requested of the host, not attempted.

Bake toolchain and CA trust into the image (or equivalent) at *build*
time. Runtime downloads of large binaries through the MITM hop are how
you get flaky agents and timeout loops.

## Gateway contract

The gateway is the product. Isolation and the agent CLI are adapters.

**Vault.** Real secrets live only here, host-mounted, not in the worker.
Each secret has a placeholder, optional env key, worker scope, and
allowed hosts. Creating a secret seeds substitution rules from those
hosts (`*` only when the operator explicitly allows every host).

**Substitution.** Last hop, after TLS terminate, before upstream TLS.
Rule key is `(placeholder, host)`. Placeholder with no rule → block and
capture. The request log stores placeholder names and redacted samples,
never real values.

**Inspection.** Only traffic you can TLS-terminate is inspectable. You
cannot MITM SSH and still rewrite credentials; crypto forbids it. SSH
and other raw streams are out of bounds, not “TODO proxy.”

**Enforcement.** The worker’s network must have no path around the
gateway (internal network, no default route, no IMDS). A proxy env var
without that enforcement is advisory.

**Ingress.** One host listener, dispatch on name, to `worker:port`.
Stable labels — not git branch, not commit. Websocket/SSE must not
buffer forever. Loopback-only servers inside the worker need an explicit
forward; do not bind them on all interfaces “to make ingress easier.”

**Control plane.** Host-trust UI/API: secrets, patterns, captures,
workers, kill-switch. The worker API is a different listener, reachable
only from the worker network, never published to the host.

## Policy pipeline

For every decoded request, in order:

1. **Internal target** (loopback, link-local, RFC1918, metadata) →
   block, except names the gateway itself must reach.
2. **Denied domain** → block.
3. **Credential-shaped content** (exact match against stored real
   values, *or* a regex library hit) → block, unless an operator
   exception matches.
4. **Placeholder present** → substitute if a rule exists; otherwise
   block as retryable (no rule yet).
5. Else **allow**.

Blocked responses to the worker carry an id and a short reason, not the
secret or the matched pattern. The full story lives on the gateway.

A comprehensive, updatable regex library matters: a leaked key must not
ride out even if the worker never saw the vault.

## Block → fix → retry

This loop is load-bearing. Without it, every miss becomes a stuck agent.

1. Worker request is blocked (no rule, looks like a real secret, MCP
   not logged in, …). Gateway captures the request, returns a retryable
   error with an id.
2. Human adds a secret, exception, or completes login on the host.
   Matching captures are replayed automatically under **fresh** policy.
   A stale verdict is never trusted.
3. If still blocked, the human retries that id from the control plane.

The agent does not get a real token and retry. The agent does not
“just curl again.” The captured request is the object.

## Out of bounds (host action)

When the worker cannot do a step without breaking the contract, it
stops guessing and asks the host:

- Exact command the host should run.
- Rendezvous path *inside the project mount* for the result.
- A one-line activity event so the request survives the session.

Later, the agent checks the rendezvous path. Missing → ask again, never
block the box, never retry the forbidden operation. Nested isolation
(containers in the worker) is this path, not a clever workaround.

## Activity

The board is a stream of short typed events from workers, not a chat
log. Kinds are the lingua franca; the agent CLI is not.

Typical kinds: started work, finished something, blocked on the outside
world, waiting on a human choice, out-of-bounds host request, idle.

Rules that keep it usable: one line, no credentials, no file dumps, not
per tool call. A monitor (human or LLM) may poke the human about idle
or blocked sessions. That monitor is a third driver, not the coding
agent.

## Adapter surfaces

Implement these. Do not leak their internals into the contract.

**Runtime.** Ensure isolated network · build/pull image · create ·
destroy · exec with TTY · inspect identity · logs. Hardening (no new
privs, dropped capabilities, read-only root, no host sockets) lives
here.

**Agent.** Import host keys into the *gateway*, write placeholders into
the worker. Seed global instructions (this contract + live
capabilities). Launch interactive / launch prompt / list sessions /
detect a live process. Headless and TTY are both required or you cannot
run a monitor or a one-shot.

**Host glue.** Hosts file or local DNS, privilege prompt, “open this
URL,” installing the gateway CA in the *browser’s* trust store. This is
OS-specific and must not enter the worker.

**MCP / OAuth (optional).** Remote tools that speak OAuth: the gateway
is the OAuth client. Worker announces a URL, gets a placeholder, writes
a bearer client config. Login and tokens happen on the host. Connect
before login is a retryable block, then replay.

## Hard rules

- Real credentials never enter the worker. Not for a moment. Not in a
  debug log.
- The worker cannot reach the network except through the gateway.
- If you cannot inspect it, you do not allow it (or you classify it as
  host action).
- Placeholders are scoped: a worker that cannot see a placeholder must
  treat that secret as blocked.
- Substitution is last-hop and host-bound. A placeholder valid for
  `api.example.com` is not valid for `evil.example`.
- Blocked work is a captured request, not a forgotten error.
- Do not nest sandboxes inside the worker.
- Do not download the agent or its plugins through the MITM hop on
  first start. Bake them.
- Do not put git branch or commit into public ingress names.
- Do not run the agent on the host “just this once.” That is a
  different, weaker product.

## Anti-patterns

- Proxy environment variables as the only enforcement.
- Letting the agent start its own OAuth and store a real bearer.
- GUI IDEs as the agent driver (they own windows and credential stores).
- Cloud sandboxes that own the network, unless you invert the topology
  (sandbox dials *out* to your gateway, or you co-deploy the gateway).
  The enforcement point in this pattern is “worker net has one door.”
- WASM or other runtimes that cannot run general TLS-using tools — a
  different product.
- One mega driver that is both the VM and the coding CLI.
- Teaching the agent to probe forbidden protocols until they hang.

## Done when

A hostile or confused agent can run tests, call allowed APIs, and serve
a preview, and still cannot:

- print a real key,
- reach IMDS or the LAN,
- write outside the project mount,
- skip the gateway.

Adding a new isolation runtime or a new agent CLI does not change the
gateway, the policy pipeline, or the activity board.
