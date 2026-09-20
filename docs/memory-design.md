# Persistent Cross-Host Fact Memory for AI Agents

## The Problem: Retrieval Is Not Memory

A retrieval corpus answers the question "what does the documentation say?" A memory answers a different set of questions: "what did we learn, who learned it, is it still true, and has anyone corrected it since?"

These are not the same thing, and conflating them produces systems that behave as though they remember when they do not.

A retrieval corpus is a static read medium. Documents are indexed, chunks are embedded, and queries return the most similar chunks. Nothing writes back. An agent that discovers a misconfiguration cannot record that discovery in the corpus so that another agent on another host avoids the same trap. An agent that learns a fact is wrong cannot mark it so. The corpus on Monday is the same corpus on Friday unless a human re-indexes it. It has no notion of time or confidence or authorship at the fact level. It cannot expire anything.

This was measured concretely on one production deployment in 2026: a retrieval corpus of approximately 94,000 embedded chunks contained exactly 16 durable facts — things an agent actually learned at runtime that no document had recorded. Everything else was documentation that already existed before any agent ran. The corpus was not memory. It was a read-only reference shelf, and the 16 facts were invisible to any agent that needed them.

The contrast is sharp. Memory has four properties a corpus lacks:

1. **Authorship.** A fact has a source: which agent wrote it, on which host, at what time. A document chunk has a file path and a page number.

2. **Confidence.** A fact can be uncertain. A model's inference and a log-derived measurement are not equally trustworthy. A corpus treats all chunks as equally valid.

3. **Temporal validity.** A fact can expire. "The staging database is at version 14" is not true forever. A corpus does not know that anything in it might have stopped being true.

4. **Writeback.** An agent reading memory can also write to it. The next agent on the next host can read what the first agent learned. A retrieval corpus is closed to runtime writes — or if it is not, it has no mechanism to distinguish a correction from a contradiction.

The rest of this document describes a data model that satisfies all four properties and explains why each design decision was made the way it was.

---

## The Data Model

### A Fact as a Durable Assertion

The central record is a **fact**: a discrete, typed assertion about the world, attached to provenance and confidence.

Each fact record carries:

- **content** — the assertion itself, in human-readable text
- **fact_type** — a category label (e.g., `config`, `credential_status`, `version`, `observation`, `correction`)
- **agent_id** — which agent wrote it
- **host_id** — which host that agent was running on
- **recorded_at** — when it was written, in UTC
- **confidence** — a value in [0.0, 1.0] indicating how trustworthy the fact is
- **expires_at** — an optional timestamp after which the fact should not be served (evaluated on read, not by a background job)
- **status** — one of `active`, `superseded`, or `tombstoned`
- **superseded_by** — a foreign key to the fact that replaced this one, if any
- **source** — a tag indicating whether this fact came from an automated feed, a model proposal, or a human promotion

This schema is intentionally verbose. Every field except `expires_at` is required at write time. Verbosity is the point: a fact that omits its source or confidence is not a fact, it is a rumour.

### Correction by SUPERSEDE, Not Mutation

When a fact turns out to be wrong, the correct response is not to update the content field in place. The correct response is to write a new fact and mark the old one as superseded.

The old record is retained in the database. Its status changes from `active` to `superseded`, and its `superseded_by` field points at the new record. Queries that return active facts skip superseded ones. But the superseded record is still there, readable to anything with sufficient privilege.

The reason for this is not merely auditability in the abstract. It is that mutation destroys the ability to distinguish two different things: a correction and a contradiction.

A correction occurs when new evidence shows the old value was wrong. A contradiction occurs when two sources disagree and neither is clearly right. If you mutate in place, you cannot tell which happened. The old value is gone. You cannot ask "what did the agent on HOST-B believe on Tuesday before the agent on HOST-A corrected it?" You cannot investigate whether the correction was itself an error. You cannot reconstruct the sequence of events that led to the current value.

Audit is not a luxury feature. When an agent acts on a bad fact and causes an incident, the first question is always "where did that belief come from and why did it survive?" A mutable fact store cannot answer this. A SUPERSEDE chain can.

There is a secondary benefit. A superseded record carries its original `confidence` and `agent_id`. If you discover that a particular agent has a pattern of writing facts that later get superseded, you can down-weight its future output. You cannot derive this from a mutable store because the evidence of error was overwritten.

### Expiry Evaluated on Read

Facts that carry an `expires_at` value are not deleted by a background job when that time passes. They are filtered out at query time.

The alternative — a scheduled reaper that deletes or archives expired facts — has a failure mode that is hard to observe. If the reaper does not run (because the job scheduler failed, because the host was rebooted, because the container was replaced), expired facts continue to be served until someone notices. The failure is silent. No query returns an error. No log entry says "I am returning a fact that should have expired 14 hours ago."

Expiry on read has no such failure mode. Every query checks `expires_at` against the current time. If the fact is expired, it is not returned. The check happens in the same transaction as the read, with no external dependency.

The expired row is still in the database. This is intentional for the same reason superseded rows are retained: the row carries provenance. "At time T, agent A believed X, but that belief was only valid until time T+delta" is a statement about the system's history. Deleting the row silently discards that statement.

An expired fact can still be queried directly by its ID for audit purposes. It is invisible to normal fact queries.

### Tombstones and Genuine Erasure

Deletion presents a conflict between two legitimate requirements: the audit record and the right to remove content.

The default deletion path sets `status` to `tombstoned`. The content field is replaced with a short message indicating when and by what authority the tombstone was applied. The row is retained. Normal queries skip tombstoned rows. Audit queries can see that a fact existed at a given time, who wrote it, and when it was tombstoned, but not what it said.

When the content itself must not persist — a credential accidentally written as a fact, personal information that should not have been stored — a second path performs genuine erasure: the content field is overwritten with a fixed placeholder, the `fact_type` is set to `erased`, and the row's status is set to `tombstoned`. The provenance metadata (agent, host, timestamp) is preserved. The content is gone. This is a destructive operation and should be logged outside the database at the time of application.

The distinction matters. Tombstoning without erasure is an audit-preserving soft delete. Erasure is a compliance operation. They are not the same action and should not share a code path.

### Full-Text Search Instead of Vectors

The query interface is full-text search, backed by SQLite's FTS5 extension. There are no embeddings, no vector index, no similarity search.

This is a deliberate trade with specific costs.

Full-text search does exact-ish matching. "Staging database version" finds facts that contain those words. It does not find "the dev DB is on Postgres 14" if neither "staging" nor "version" appears in that fact. Semantic similarity, synonyms, and paraphrase are all outside the model. A query must use words that appear in the facts it is looking for.

The cost is real. An agent looking for facts about "the authentication service" will not find facts written as "the auth service" unless both terms are present. This requires discipline in how facts are written: use consistent, predictable vocabulary. That discipline is easier to enforce in a small, curated fact set than in a large corpus.

The benefit is also real. FTS5 is fast, deterministic, and debuggable. Given a query, you can reason exactly about which facts it will and will not return. There is no embedding model to retrain, no drift between the model used at write time and the model used at query time, no dimensional mismatch when the embedding model is updated. The index is stored in the same SQLite file as the facts. The entire service runs in 11.6 MB of resident memory with a 512 MB cap. There is nothing to operate.

The trade is right when: the fact set is small and high-value (dozens to low thousands of facts, not millions), the facts are written by automated feeds with controlled vocabulary, and human-readable exact matching is sufficient for the agents querying them. It is wrong when: the fact set is large and uncontrolled, the source vocabulary varies widely, or semantic similarity is genuinely necessary for recall.

On the production deployment where this was measured, the fact set was small enough that full-text search over a small number of records completed in negligible time. The alternative was operating a vector database for a workload that did not require it.

---

## The Write Path

### No Model in the Automated Feed

The automated feed — the process that turns logs and structured outputs into facts — contains no model calls. It is a deterministic extraction: specific fields from structured log records become specific facts. The mapping is static code, not inference.

This is the most important constraint in the design.

A model that can write directly and unconditionally to memory will eventually write a hallucination. Not because the model is malicious or poorly prompted, but because hallucination is a property of the technology. Given enough calls, a plausible-but-false assertion will appear in the output. If that output goes directly to memory, the next agent reads the hallucination as fact. The next agent may act on it. A correction requires someone to notice that the fact is wrong — which requires someone to look — and the damage accumulates between the moment of writing and the moment of correction.

The automated feed avoids this by being deterministic. If a log record says the deploy completed at a given time with a given SHA, the fact records exactly that, derived mechanically. There is no inference step. The fact is either derivable from the log or it is not written.

### Model-Proposed Facts Land in Quarantine

When a model does need to write a fact — an observation, an inference, a pattern it detected — the write lands in a quarantined state: low confidence, tagged with `source=model_proposed`, and excluded from active fact queries by default.

A quarantined fact is not treated as true. It is a proposal. It becomes a true (active, normal-confidence) fact only when a human reviews it and promotes it. The promotion action changes the `source` to `human_promoted`, raises the confidence to whatever the reviewer assigns, and makes the fact visible to normal queries.

This creates overhead. A model that proposes ten facts per hour requires ten human promotions per hour to make any of them useful. In practice, the rate is much lower, and the promotion interface is lightweight enough that the overhead is acceptable. The alternative — allowing models to write directly at full confidence — is not acceptable because it has no bound on the rate at which bad facts can accumulate.

The quarantine design also produces a useful dataset over time: the set of model-proposed facts that were not promoted is a record of what the model got wrong or what it proposed that turned out to be unverifiable. This dataset can inform prompt design and model selection.

### Idempotency

The automated feed is designed to be run repeatedly without producing duplicate facts. On the production deployment, six consecutive live runs after the initial backlog drained wrote zero new facts.

Idempotency requires a stable identity for each fact. The feed computes a deterministic key from the combination of: the fact type, the content (normalized), the source log record identifier, and the agent ID. Before writing, it checks whether a fact with that key already exists in the `active` or `superseded` state. If it does, the write is skipped. If it does not, the write proceeds.

This means the feed can be scheduled to run every 15 minutes, on every host, without coordination. Runs overlap. The same log record is processed multiple times. No fact is written twice. The safety follows from the identity check, not from distributed locking or run-once guarantees, which are fragile.

The consequence is that the feed can fail partway through and be safely restarted. It can be run from a cold start on a new host and will only write facts that are not already present in the shared store. This property is load-bearing: without it, every feed failure becomes a potential data quality incident.

---

## Cross-Host Correctness

The design only works if facts written on one host are readable on another and corrections propagate in both directions. This is not automatically true and requires a test.

The cross-host test is:

1. Start with a shared memory store visible to HOST-A and HOST-B.
2. Write fact F on HOST-A with content "X is true", confidence 0.9, status `active`.
3. Read the store on HOST-B. Confirm that fact F is visible and returns "X is true".
4. On HOST-B, write a correction: a new fact F' with content "X is false", superseding F. The old fact F has its status changed to `superseded` and its `superseded_by` field set to F'.
5. Read the store on HOST-A. Confirm that a query for the same fact type and subject returns F' ("X is false") and does not return F. Confirm that a direct query by F's ID returns F with status `superseded`.

Step 5 is the proof. If HOST-A returns the old value, the store is not shared, or the SUPERSEDE did not propagate, or there is a caching layer that has not been invalidated. If HOST-A returns the new value and can still retrieve the old one by ID, the design is working as intended.

The test does not require both hosts to be running simultaneously or to communicate directly. They communicate through the shared store. The store is the only source of truth. There are no per-host caches that the test could pass while real caches fail, because there are no per-host caches.

---

## Cost Context

This section records the cost incident that motivated the memory service design and explains what it implies about context management more broadly.

On one agent-harness day, 688 model calls cost $53.09. The expensive tier ran at $0.88 per call and the cheaper tier at $0.36 per call, against $0.028 for a lower-cost model available for the same task. The expensive tier cost 13 times more than the cheapest available option, which is not itself unusual, but when a cheaper model costs 13 times more than an even cheaper one, the model is not the variable driving cost. The input is.

The root cause was staged context files. Each call staged between 8 and 20 context files, each capped at 200 KB. A single call could therefore carry several megabytes of input. The cap was lowered to 24 KB per input file, with explicit head+tail truncation and a marker indicating where content was cut.

The mechanism that makes large per-call context especially expensive is prompt caching. Measured on one production deployment: on a six-word prompt, a call billed 10 input tokens, 13,689 cache-read tokens, and 17,327 cache-creation tokens. Cache reads bill at roughly 0.1x the base input rate. Cache writes bill at roughly 2x the base input rate. A stable prefix that is written into the cache once and read many times is nearly free. Novel bytes written per call are expensive in two ways: they are billed at 2x when written into the cache, and because the next call stages different bytes (different context files, different outputs to review), the cached entry is never re-read, so the 2x write cost is not amortized.

The memory service is designed with this in mind. A small fact set, returned as a few hundred bytes of context per call, is a stable or near-stable prefix: the same facts appear in many calls, the cache entry is written once, and subsequent reads are cheap. A retrieval corpus that returns different chunks per call is the opposite: each call writes a new cache entry at 2x and never reads it back.

The memory service's 11.6 MB resident footprint and full-text search model are not compromises forced by resource constraints. They are the correct operating point for a workload with a small number of high-value facts, where the goal is to add a few hundred bytes of reliable context to each call, not to retrieve arbitrary documents.
