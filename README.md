# factmem — persistent, correctable memory for AI agents

Most "agent memory" is a vector index over a pile of documents. That answers *what does the
documentation say*. It does not answer *what did we learn, who learned it, and is it still true*.

The difference is not academic. One production deployment ran a retrieval corpus of roughly
**94,000 embedded chunks** that contained **16 durable facts**. Nothing wrote back to it
automatically, nothing expired, nothing was ever corrected, and an agent on one host could not
read what an agent on another host had learned an hour earlier. It was a very good search index
and it was not a memory.

`factmem` is the other half: a small, boring, auditable store of **durable assertions** with
provenance, confidence, correction history and expiry — reachable by any agent on any host over
HTTP or MCP.

It is deliberately unexciting. One Python file, one SQLite database, no vector index, no
embedding model, no framework. It runs in about **12 MB of RAM** under a 512 MB cap. You can read
the whole thing, and you can restore it at 3am from a single file.

---

## What it does

- **Facts, not chunks.** A fact is an assertion with a source (which agent, on which host, when),
  a confidence, a type, and an optional expiry.
- **Correction supersedes, it does not overwrite.** When a fact changes, the old value is retained
  and hidden rather than mutated. You keep the ability to tell a *correction* from a
  *contradiction*, and to audit why an agent believed something last week.
- **Expiry is evaluated on read.** There is no reaper job to fail silently at 4am, and no window
  where an expired fact is still being served. The row survives for audit; it simply stops being
  returned.
- **Deletion is a tombstone** — with genuine text erasure available for the case where the content
  itself must not persist.
- **Cross-host by default.** Write on one machine, read it on another. The test that proves it is
  in the docs, and it is the test worth running: write on A, read on B, correct on B, read back on
  A and see the old value superseded.
- **No model in the write path.** See below — this is the design decision that matters most.

## What it deliberately does not do

- **No vectors, no semantic search.** Full-text search only. This is a real trade: you lose
  "find me things that mean roughly this". It is the right trade for a small, high-value,
  human-readable fact set, and the wrong one for a document corpus. Keep your corpus; this is not
  a replacement for it.
- **No framework integration, no agent runtime, no orchestration.** It is a store with an API.
- **No clustering, no replication.** One instance, one file, backed up.

---

## The decision worth defending: no model writes to memory

Anything a model proposes lands **quarantined** — low confidence, explicitly tagged, and not
treated as true until a human promotes it. The automated feed that writes facts without review
derives them *deterministically* from logs, with no model call anywhere in the path.

The reasoning is simple. A model with direct write access to shared memory will eventually write
a hallucination, and every later agent will read it as established fact, cite it, and build on it.
Memory corruption in an agent system is not a crash — it is quiet, it compounds, and by the time
you notice, you cannot tell which downstream conclusions were poisoned.

We watched a cheap model fabricate an entire table of repository names, star counts and dates,
and watched the automated evaluator **pass** it, because the output had the right *shape*. That is
in [`docs/failure-modes.md`](docs/failure-modes.md) along with six other failures that each left
a guard behind.

---

## The token-economics half

This repository also carries what we learned about **why agent systems burn money**, because it
turned out to be the same kind of problem: an assumption nobody had measured.

One harness day cost **$53.09 across 688 calls**. The expensive tier cost $0.88/call — and the
*cheap* tier cost **$0.36/call**, against $0.028 for a third-party model. A cheap model costing
13x a cheaper one cannot be explained by the model. It is the input.

The cause was staged context: files capped at **200 KB each**, 8–20 per call. The fix was a 24 KB
cap with an explicit truncation marker (a *silent* chop is worse than useless — a model will quote
across the gap as though the text were contiguous).

The counter-intuitive part came from measuring instead of assuming. The agent CLI already applies
prompt caching itself, at a **1-hour TTL** — on a six-word prompt, 10 billed input tokens against
13,689 read from cache. So the stable prefix was already nearly free, adding more caching
structure would have gained nothing, and **novel per-call bytes were the entire bill**. Worse,
large per-call context gets written into the cache at ~2x and then never re-read, because the next
call stages different bytes.

One CLI call with JSON output settled a question that documentation could not.
Full write-up: [`docs/token-economics.md`](docs/token-economics.md).

---

## Repository layout

```
src/factmem.py         the service: HTTP API, SQLite/FTS5, selftest
src/factmem_mcp.py     MCP stdio server (write, search, get, correct, forget, health)
src/factmem-review     CLI to triage quarantined proposals (list/promote/reject/purge)
docs/memory-design.md  the data model and why each decision was made
docs/architecture.md   runtime shape, deployment, isolation
docs/token-economics.md why input volume is the bill, and the techniques that cut it
docs/failure-modes.md  seven real failures and the guard each one left behind
```

## Quick start

```bash
python3 src/factmem.py --selftest     # no deps beyond the standard library
python3 src/factmem.py --serve        # binds to a private interface; see docs/architecture.md
```

The service **refuses to start** on a public bind address rather than warning about it. That is
intentional: a memory store is exactly the thing you do not want to discover on the open internet
six months later.

## Requirements

Python 3.11+ standard library. That is the entire dependency list — `sqlite3`, `http.server`,
`json`, `re`. No pip install.

---

## Status and provenance

This is extracted from a running single-operator deployment, generalized for publication.
Hostnames, paths and credentials from that deployment are not in this repository by design.

Every number quoted here was measured on that one deployment. They are offered as evidence that
the problems are real, **not as benchmarks** — your corpus, your model mix and your prices will
differ, and the diagnostic method in `docs/token-economics.md` will serve you better than our
figures will.

The production feed that derives facts from operational logs is deployment-specific and is not
included; the *pattern* it implements is documented in `docs/memory-design.md`.

## Licence

MIT. See [`LICENSE`](LICENSE).
