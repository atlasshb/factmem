# Token Economics for Agent Systems

Most cost optimization advice for LLM-backed systems focuses on model choice: use a cheaper model
and pay less. That advice is correct in isolation, but it misses the dominant variable in agentic
systems. When an agent calls a model repeatedly with large staged context, the input token count
dwarfs the per-token price difference between models. The result is a system where switching from
an expensive model to a cheap one barely moves the bill, while fixing context size cuts it
dramatically.

This document explains why, what to do about it, and how to instrument a system so the next
incident is diagnosable from logs rather than from guesswork.

---

## The Diagnostic Move: Divide and Infer

When you get an unexpected bill, resist the urge to immediately swap models or add caching.
Instead, do the arithmetic first.

Take your total spend for the period and divide it by the number of model calls, broken out by
tier. That gives you an average cost per call at each tier. Then divide that figure by the
published per-token price for the tier.

The result is the implied average context size in tokens for each tier. If that number is in
the hundreds of thousands, the input is the problem. If it is small, look elsewhere.

The tell is when a cheap model produces an absurd per-call cost. A cheap model cannot be
expensive because of the model. If you see $0.36 per call on a cheap tier, where the model
price is $0.028 per thousand tokens, the only explanation is that each call is carrying an
enormous context. The model choice is irrelevant at that scale.

Measured on one production deployment in 2026: one agent-harness day cost $53.09 across 688
calls. The expensive tier cost $0.88 per call. The cheap tier cost $0.36 per call. A third-party
model with a published price of $0.028 per thousand tokens. A cheap model costing 13 times what
a cheaper alternative would suggests enormous input, not model inefficiency.

The root cause, once found, was straightforward. Staged context files were capped at 200 KB
each, with 8 to 20 files per call. A single call could therefore carry several megabytes of
context. Lowering the cap to 24 KB per input, with head-and-tail truncation, addressed the cost
at the source.

---

## Why Input Dominates in Agent Systems

A single-turn chatbot sends a short user message and receives a response. The input is small by
construction. An agent is different: before each model call, it assembles a context packet that
may include a system prompt, retrieved documents, prior conversation turns, structured state
files, tool call history, and reflection output from previous failed attempts. Each of these
individually seems reasonable. Composed, they produce inputs that are orders of magnitude larger
than the system was designed to handle cheaply.

The asymmetry is that input costs scale linearly with every byte you send, while output costs
are bounded by what the model actually needs to produce. An agent doing classification or
extraction may produce 200 output tokens while consuming 50,000 input tokens. At that ratio,
optimizing output is noise.

There is a second asymmetry when prompt caching is involved. Cache writes typically bill at
around twice the standard input rate, and cache reads bill at roughly one-tenth. A stable prefix
is nearly free to read, but a per-call context that changes every call is written into the cache
at 2x and never read again, because the next call stages different bytes. Large per-call context
is therefore worse than it appears: you pay the 2x write penalty without ever collecting the 0.1x
read benefit.

This was confirmed by measurement rather than inference. On a six-word test prompt, the agent
CLI produced: 10 billed input tokens, 13,689 cache-read tokens, 17,327 cache-creation tokens.
The cache mechanism was already active, and the cache was hitting, but it was hitting on the
stable system prompt prefix, not on the per-call staged context.

---

## Techniques That Actually Reduce Cost

### Cap staged context per input, with explicit truncation

The direct fix for the incident above. Every document or file fed into a model call should have
a hard size cap, and the cap should be enforced with a head-and-tail strategy: take the first N
bytes, add a marker like `[... truncated, N bytes omitted ...]`, then take the last M bytes.

The marker matters. A silent chop — feeding the first N bytes with no indication that the text
continues — causes the model to treat the content as complete. If the model is asked to quote
or summarize, it will quote across the gap as though the text were contiguous, which produces
hallucinated context. The truncation marker tells the model that it is working with a partial
view and it can reason accordingly.

24 KB per input is a usable starting point. It is large enough to capture most log excerpts,
code files, and document summaries, and small enough to keep per-call context at a manageable
scale even when 10 to 20 files are staged.

### Stage a generated index or summary instead of a file tree

A raw file tree has low information density. Most of the bytes are paths, and a model rarely
needs to know every path to complete a task. If your agent assembles context by scanning a
directory and sending the listing, replace that listing with a generated summary or index: a
few hundred words describing what exists and where to find it, written once and updated
incrementally.

The cost difference is significant for large repositories or large output directories. A file
tree for a system with thousands of artifacts may run to hundreds of kilobytes; a summary may
be two or three paragraphs.

### Prompt-prefix caching and what must be byte-identical

Most hosted model APIs offer some form of prompt caching. The mechanism varies, but the
invariant is consistent: the cached prefix must be byte-identical to the prefix in previous
calls. A single character difference, a timestamp injected into the system prompt, or a version
string that changes per build will break the cache.

Design your system prompt to be stable. Put all dynamic content — task-specific context,
per-call state, user input — after the stable prefix, never before or inside it. If your system
prompt contains anything that changes between calls, extract it to the user message or to a
section that comes after the cacheable block.

When the prefix is already cached and hitting, adding more caching structure to that same
prefix is worthless. The reads are already cheap. The only lever left is reducing the novel
bytes that come after the cached prefix, because those bytes are not cached and are charged at
full input rates plus the 2x write penalty.

### Session continuation instead of re-sending history

An agent that reconstructs its full conversation history on every call by replaying prior turns
from a database is paying full input cost for the history every time. Session continuation —
resuming a conversation within the same session context — avoids this. Prior turns are in the
model's context window already, and the cache handles them.

The constraint is session lifetime. If a session expires between calls, continuation is not
possible and you must re-send. Design agent workflows to complete within a session where
possible, and be aware of the TTL on whatever caching mechanism you are using. A one-hour TTL
means that any workflow running longer than an hour will face a cache miss on the history at
the boundary.

### Routing by task complexity to a tiered model ladder

Not every call needs the most capable model. A call that classifies a log line into one of five
categories does not need the same model as a call that synthesizes a technical explanation
across multiple documents.

A tier ladder assigns model capability to task complexity. Simple, high-confidence classification
tasks go to cheap, fast models. Complex reasoning, synthesis, and generation tasks go to capable
models. Routing decisions are made before the call, based on the task type, not after.

The economics are favorable when high-volume, low-complexity tasks are separated from
low-volume, high-complexity ones. In the incident above, the cheap tier was still expensive
because of input size, not because it was the wrong tier for the task. Routing alone does not
fix a context problem.

### Not re-sending prior failure reflections in full

When an agent fails a task and reflects on why, that reflection is often written back into the
context for the next attempt. If the full reflection is re-sent on every subsequent call, it
grows with each attempt and is never pruned.

Cap reflection context the same way you cap document context. Keep a summary of the failure
reason and the corrective action, not the full reflection text. After a fixed number of
attempts, discard old reflections entirely: if the agent has not succeeded in three tries, the
early reflections are not helping.

### Measuring rather than assuming

The two most important questions about caching — whether it is active and whether it is hitting
— are not answerable from documentation alone. The CLI or API usage block tells you, per call,
exactly how many tokens were cache reads, cache writes, and uncached input.

A single CLI call with JSON output on a test prompt settled a caching question in the deployment
above that documentation had not resolved. The numbers showed the cache was hitting on the
stable prefix, which meant that prefix optimization was already done and the cost problem lay
entirely in per-call context.

Record token counts per call in your logging infrastructure. A system that logs only latency
and HTTP status codes can only infer cost from the bill at the end of the month. A system that
records input tokens, cache-read tokens, cache-write tokens, and output tokens per call can
diagnose an incident in minutes by filtering on calls with high input counts.

---

## What to Record Per Call

For each model call, persist at minimum:

- Timestamp and call identifier
- Model and tier
- Input token count (uncached)
- Cache-read token count
- Cache-write token count
- Output token count
- Task type or pipeline stage
- Whether the call succeeded or was retried

From these fields you can reconstruct cost per call, identify the calls that drove the bill,
correlate high input counts with specific pipeline stages, and verify that caching is behaving
as expected.

The cache-write field is particularly useful. A high cache-write count with a low cache-read
count on the same call type means you are paying the 2x penalty repeatedly without collecting
the 0.1x benefit. That is the signature of context that changes every call.

Without these fields, you can only observe the total bill and theorize. With them, you can sort
your calls by input token count and look at the top ten. The answer is usually in the first row.

---

## The Counter-Intuitive Result

When you first apply prompt caching to a system, the impact is dramatic. Stable prefixes become
cheap, and costs drop substantially. It is tempting to conclude that the answer to any future
cost increase is more caching.

It is not. Once your prefix is cached and hitting, you have already extracted the value that
caching offers. A cached prefix costs roughly one-tenth to read. Adding more structure to that
prefix does not make it cheaper; it is already nearly free.

At that point, the only lever that matters is the novel bytes after the cached prefix. Those
bytes are the user input, the staged context, the retrieved documents, and the per-call state.
They cannot be cached because they change every call. They are charged at full input rates plus
the cache-write penalty.

The implication is that caching and context reduction are not alternatives; they address
different cost components. Caching eliminates the cost of the stable prefix. Context reduction
eliminates the cost of the per-call novel bytes. Once caching is in place, further optimization
is almost entirely about what you are sending in the non-cached portion of each call.

The diagnostic arithmetic tells you which problem you have. If the implied context is enormous
on a cheap model, you have a context problem, and caching will not fix it.
