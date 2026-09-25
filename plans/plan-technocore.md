# Technocore plan — overview

## Purpose

The technocore is a user-directed network of AI workers that develops and
operates web applications, phone applications, servers, databases, domains,
and related infrastructure. It preserves user intent across sessions and
machines, coordinates work, shares knowledge, and keeps consequential actions
under user control.

The mental model is a worker mycelium—or a fleet of different Culture-style
ship designs—held together by gateways and a central coordination server.

## Topology

- **Server** — maintains accounts, identities, fleet topology, permissions,
  directives, plans, tasks, approvals, resource inventory, and an event stream.
  It stores encrypted secrets but cannot decrypt them.
- **Gateway** — a deliberately simple trusted boundary between local workers
  and the server. It synchronizes authorized encrypted credentials, substitutes
  secrets into requests, and transports worker communication. It makes no
  planning decisions.
- **Worker** — an identity-bearing agent that executes bounded work, reports
  progress, requests help, and participates in the shared knowledge systems.
- **Web client** — an observation and control surface. It signs approvals and
  can decrypt/edit authorized secrets, but performs no worker tasks.

A user may enroll any number of gateways on laptops, workstations, or cloud
machines. Workers appear beneath their gateway in the global fleet.

## Worker hulls

Different environments provide different intrinsic capabilities:

- **Local module** — a worker in a hardened container protecting a valuable
  host. It has narrow mounts and no Docker control, and requests host actions
  when it reaches the sandbox boundary.
- **Cloud ship** — a disposable EC2 machine whose network is restricted by an
  external egress/MITM gateway. The worker may have root, install software,
  launch containers, and freely shape its local machine. Cloud credentials and
  access to other systems remain delegated capabilities.

Piso describes the hull's capabilities rather than artificially gating local
operations. A subprocess or container only becomes a technocore worker when it
receives an identity and registers; that registration may require approval.

## Identity, pairing, and encryption

Gateways, workers, web clients, and agent roles have generated Nostr
identities. A new client or gateway can display a QR code that an authenticated
phone client scans to pair it with the user's account. The production server
URL is a built-in default, with an explicit override for development or
self-hosting.

Secrets and MCP credentials use envelope encryption:

1. A random key encrypts each object.
2. That key is wrapped for each authorized web client or gateway.
3. The server stores ciphertext and recipient envelopes.
4. Workers and the server never receive plaintext or decryption keys.

Revoking an envelope prevents future retrieval but cannot make a gateway
forget plaintext it already used; strong revocation requires credential
rotation.

## Roles

- **Worker** — performs repository and operational tasks.
- **Monitor / PM** — tracks directives, goals, plans, blockers, leases, and
  completion. Gateway monitors may coordinate under a global monitor.
- **Booster Curator** — identifies reusable knowledge and proposes booster
  creation, updates, merging, tagging, or archival.
- **Locutus** — handles Telegram communication, translating messages into the
  same commands and approvals used by web and CLI clients.

Monitors and curators propose actions within policy; they do not bypass user
approval. Leases prevent multiple monitors or workers from duplicating work.

## User intent and events

The system is driven by signed user inputs from worker conversations, web
clients, CLI, Telegram, QR pairing, and approval actions. Inputs become durable
commands and events.

Long-lived intent is represented as:

- **Standing orders** — persistent rules and recurring responsibilities.
- **Directives** — goals with boundaries and completion criteria.
- **Plans** — versioned steps and dependencies.
- **Tasks** — bounded assignments leased to workers.

Incomplete directives can continue across sessions and machines. Budgets,
concurrency limits, expiration, stop conditions, and approval requirements
bound autonomy.

Human action requests cover work the system cannot perform, such as adding a
machine, changing DNS, completing domain verification, or publishing an app.
They can be completed, rejected, deferred, or failed; workers subscribe to the
result and adapt.

## Knowledge systems and sources of truth

The primary sources have different authority:

1. **User intent** controls goals, constraints, priorities, and approvals.
2. **Git repositories** describe current code and project-local behavior.
3. **Boosters** provide curated, versioned, reusable guidance.
4. **Hammerspace** holds informal memories, observations, conversations,
   handoffs, and notes that may be stale.

All recalled knowledge carries provenance, scope, time, and version. The
curator can promote repeated or valuable Hammerspace knowledge into a booster,
subject to approval.

AI Boost remains the booster store and MCP retrieval system. The technocore
adds gateway/worker identity and collection-level permissions. Workers search,
preview, inject, and propose boosters through an automatically configured MCP
connection without receiving provider credentials.

## Worker-facing surface

A worker connects to the wider technocore through:

- Work inbox: directives, plans, tasks, leases, and handoffs
- Inter-worker and user messaging
- Hammerspace search and capture
- Booster search, injection, proposals, and access requests
- Approval, clarification, capability, worker, and human-action requests
- Application and infrastructure resource inventory
- Secret-backed capabilities exposed as placeholders, never plaintext
- Activity and status reporting

Powerful cloud workers may create their own local tools through a root shell.
Technocore tools are mainly for shared state, external authority, and
coordination.

## Applications and infrastructure

The server tracks applications, repositories, services, environments,
deployments, releases, servers, databases, storage, domains, DNS, certificates,
cloud resources, costs, and dependencies. Git should remain the desired-state
source where practical; the central inventory is a reconciled view of actual
resources.

Infrastructure autonomy expands progressively: propose, apply after approval,
then act within standing limits. Initial autonomous grants should target
short-lived test environments with explicit account, region, cost, network,
and TTL constraints. Production, IAM, domains, and destructive database
operations remain separately gated.

## Central architecture

The central service uses a relational current-state model plus an append-only,
signed event log. Major entities include principals, gateways, workers,
resources, grants, encrypted objects, key envelopes, directives, plans, tasks,
approvals, booster references, infrastructure inventory, and events.

Its interfaces separate concerns:

- REST control API for commands and current state
- Event stream for updates and coordination
- Encrypted object synchronization
- Worker-facing MCP for AI Boost knowledge

The server is authoritative for identity, topology, intent, permissions, and
coordination—not secret plaintext, Git contents, or booster contents.

## Guiding principle

Workers freely act inside their hull, but crossing into shared, costly,
sensitive, or externally consequential systems requires an explicit delegated
capability, standing policy, or user decision. The technocore should maximize
continuity and useful autonomy without obscuring who authorized an action or
where its knowledge came from.
