# Architecture

## Problem

Agent systems that retrieve from unmanaged document corpora do not retain learned facts. Each call stages raw documents, embedding vectors, or context files—large, stale, and repeated—into the LLM's input. A 688-call agent day cost $53.09 because staged context files averaged 200 KB each and called 8–20 times per request. Vector search over document stores is not memory: nothing writes back, nothing expires, nothing gets corrected, and an agent on one host cannot read what an agent on another learned.

This system trades document retrieval for durable, queryable facts. It costs almost nothing to run, exposes a standard MCP interface, and scales horizontally by credential isolation.

## Components

### Memory Service

A single HTTP microservice: Python + SQLite/FTS5, 11.6 MB resident, 512 MB memory cap.

The service owns one database file. Every write is sequential: inserts, corrections, expirations, deduplication. The index fits in RAM. Reads are lookup-only; clients never cache stale results. Recovery is human-readable: shut down the service, audit the database with any SQLite tool, restore a backup, restart. No migration scripts, no schema version negotiation, no wire protocol versioning.

The API is JSON over HTTP: `write(fact, scope, ttl)`, `search(query, scope)`, `get(id)`, `correct(id, new_fact)`, `forget(id)`, `health()`. The service binds to a private interface and refuses to start if you ask for public. No TLS termination in the service itself; clients over untrusted networks use a reverse proxy or VPN.

Credential isolation happens at the network layer: one credential per client host, validated by HTTP header. Clients are process-isolated; one corrupt agent cannot exhaust the cap or pollute another's queries.

### MCP Server

An MCP server exposes the memory service API to any MCP-capable agent. A separate process, stateless, scales horizontally. Credentials are environment variables or read from a credential store. Each server process gets a unique write scope so multiple agents writing to the same memory instance never collide on fact IDs.

### Deterministic Feed

A lightweight job runs on a timer (every 4–6 hours), queries the memory service, and synthesizes new facts from existing ones without model calls. The feed is idempotent: on six consecutive runs, it wrote zero new facts after the initial backlog drained. It exists to surface patterns that emerge after many manual corrections and writes. Feed tasks are optional; the system is complete without them.

### Proposal Stage

Facts enter a quarantined state before commitment. A proposal specifies the fact text, scope, TTL, and a reason. A human or automated triage review decides: accept, reject, or edit. Accepted proposals become facts; rejected ones disappear. Edits flow back to the proposal for re-review. This stage catches hallucinations and corrects wrong facts before they propagate to dependent agents.

### Review CLI

A CLI tool for triaging proposals. It lists pending proposals, fetches the full context of each, and prompts: accept / reject / edit. Edits are staged as new proposals and re-routed to triage. The tool formats proposals for readability and logs decisions for audit.

### Corpus Maintenance

A non-model job that deduplicates and vacuums the database monthly. One production deployment: 94,245 chunks → 83,666, 600 MB → 562 MB, zero model calls. The tool is idempotent and can run concurrently with the service; it removes only unreachable facts and obsolete embed metadata.

## Data Flow

1. An agent calls the MCP server with a fact, search query, or correction.
2. The MCP server routes to the memory service.
3. Writes are staged as proposals; the agent receives an ID.
4. A human or scheduled triage process reviews proposals.
5. Accepted proposals become durable facts in the database.
6. Searches and corrections operate on committed facts only.
7. On expiration or explicit forget, facts are soft-deleted.
8. The maintenance job periodically deduplicates and compacts.

## Deployment Model

### Why Single File and Database

Simplicity under pressure. If the memory service is unavailable, the operator can:
- Stop the service.
- Copy the database file to a laptop.
- Open it with any SQLite tool to inspect or roll back.
- Restore a backup.
- Restart.

No migration framework, no connection pooling, no schema negotiation. The entire state of the system is one auditable file. The operator owns recovery, not a vendor.

### Why Private Interface Only

The service contains durable facts about your agents, your deployments, and your business logic. It is not a public API. It binds to `127.0.0.1` or a private network interface by design. If you ask it to bind to `0.0.0.0`, it refuses at startup with a clear error. This is not a warning; it is a hard requirement. Clients on other hosts access via a reverse proxy with credential enforcement, typically on a VPN or private network.

### Process Isolation and Memory Cap

Each MCP server process runs with a 512 MB memory ceiling. If a server consumes more, the OS kills it. No OOM killer surprises; no out-of-memory crashes that corrupt the database. The service itself is capped separately. Agents in different processes cannot leak memory into the memory service.

### What a Remote Client Needs

Two things:
- An HTTP endpoint (private network address + port).
- One credential (a bearer token, API key, or identity header).

A client never needs the database file, schema introspection, or vendor-specific libraries. If you have HTTP and a credential, you have memory access.

## Context Cost and Why It Matters

A typical agent call staged 200 KB of context files, 8–20 times, across 688 calls in a day ($53.09 at expensive tiers). That context—raw documents, embedding vectors, truncated transcripts—is stale and unreusable.

The memory service lets agents write facts: "API endpoint X returns status 500 under load Y," "customer Z prefers async," "config key FOO was renamed to BAR." These facts are tiny (under 1 KB each) and reusable. At call time, an agent stages only the facts it needs, not the corpus. Measured on one deployment, the input reduction alone dropped per-call LLM cost by ~98%.

Stable context (facts you write once, read many times) is nearly free because the agent CLI applies prompt caching at a 1-hour TTL. On a six-word prompt: 10 billed input tokens, 13,689 cache-read, 17,327 cache-creation. Cache reads bill at ~0.1x; cache writes at ~2x. Large per-call context is written into the cache at 2x and never re-read.

## Limits and Non-Goals

The system does not:
- Shard the database across hosts. (Deploy multiple instances with credential isolation.)
- Serve as a vector store. (Use a specialized system for document embeddings.)
- Provide strong consistency guarantees across distant replicas. (Use an external write-ahead log.)
- Expire facts automatically based on staleness heuristics. (Operators decide TTLs; the agent corrects facts it finds stale.)

## Diagram

```
┌────────────────┐    ┌────────────────┐    ┌────────────────┐
│   Agent Host A │    │   Agent Host B │    │   Agent Host C │
│    (MCP CLI)   │    │    (MCP CLI)   │    │    (MCP CLI)   │
└────────┬────────┘    └────────┬────────┘    └────────┬────────┘
         │ Token A             │ Token B             │ Token C
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               │ (HTTP + Auth)
                               ▼
                    ┌──────────────────────┐
                    │  Memory Service      │
                    │  (Port: MEMORY_PORT) │
                    │  Python + SQLite     │
                    │  11.6 MB, 512 MB cap │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  database.db         │
                    │  (One auditable file)│
                    └──────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        ┌──────────┐   ┌───────────┐   ┌──────────────┐
        │ Triage   │   │  Corpus   │   │ Deterministic│
        │ CLI      │   │Maintenance│   │ Feed Job     │
        └──────────┘   └───────────┘   └──────────────┘
```

## Sizing for an Organization

On one production deployment (2026): a 512 MB memory service running 24/7 cost ~$40/month for compute. The database file grew at ~5 MB/week. Read latency: 5–50 ms. Write latency (including proposal staging): 20–200 ms. Throughput: 50–200 requests/second sustained.

For a small organization (< 50 agents), one service instance suffices. For larger deployments, run one instance per team or geographic region, or shard by agent credential.

## Getting Started

1. Deploy the memory service on a private network with a credential store.
2. Deploy one MCP server instance per agent host, or one per team.
3. Configure agents to connect to the MCP server with a credential.
4. Set up triage: reserve 15 minutes twice a week to review proposals.
5. (Optional) Set up a corpus-maintenance job on a monthly schedule.
