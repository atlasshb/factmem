# Failure Modes

This document describes the real failures this design exists to prevent. Each entry names the symptom, the root cause, and the guard that stops it from happening again.

## Dry-run checkpoint persistence

**Symptom:** A dry-run that was supposed to be read-only recorded its checkpoint anyway. On the first real run, the system found nothing left to process and wrote zero new facts to storage.

**Cause:** The state-save routine did not check the dry-run flag before persisting the checkpoint. It treated the dry-run iteration as authoritative progress and advanced the cursor past all work.

**Guard:** The state-save method accepts the dry-run flag as an argument and returns early without mutation if the flag is set. Real runs pass `dry_run=False`; the method inspects this before writing to the checkpoint file.

## Cheap model hallucinating structured data

**Symptom:** A cost-reduction effort switched to a cheaper language model for fact extraction. The model fabricated every repository name, star count, and date in its output. The downstream evaluator passed all rows because they had the correct JSON shape.

**Cause:** Shape-based validation checks only that a row is a dictionary with the right keys. It does not verify that the values are real. The cheaper model was confidently wrong, and the evaluator had no way to tell.

**Guard:** Facts are verified out of band against a trusted source before they enter the system. Verified data is frozen into a static file and shipped with the code rather than re-fetched on every run. The evaluator checks both shape and membership in this frozen set.

## Rate-limit exhaustion from per-item API calls

**Symptom:** Every run made one API call per item to fetch fresh metadata. On a production corpus with tens of thousands of items, this exhausted the service rate limit within hours. Subsequent calls returned "unknown" for every row until the quota reset.

**Cause:** The system re-fetched metadata on every run without caching. Each item was treated as if it could change between runs, so no data was reused.

**Guard:** Same as the fabrication guard above. Static, verified data is the source of truth. The system does not call the upstream API on every run; it only reconciles changes against the frozen baseline. This reduces call volume by orders of magnitude and eliminates rate-limit risk.

## Secret-detection regex missing trailing tokens

**Symptom:** A regex designed to detect authentication tokens in logs missed a token that appeared at the end of a sentence, followed by a period. The period was preserved in the output, leaking the token.

**Cause:** The regex used a lookahead assertion `(?!\w)` to ensure a token did not have a word character after it. A period is not a word character, so the lookahead succeeded, and the token was matched and then not redacted.

**Guard:** The lookahead is tightened to exclude only word characters (`(?!\w)` is correct), and a test case is added that places the secret in final position before punctuation. The test runs on every build.

## Silent truncation of staged context

**Symptom:** A model was asked to quote from context that had been silently truncated. It quoted across the gap as though the text were contiguous, producing nonsense that referenced both sides of a large omitted section.

**Cause:** Context files were truncated to fit memory budgets, but no marker was left to signal the gap. The model could not distinguish between "no gap" and "gap so large that the missing text is unknown."

**Guard:** Truncation always includes an explicit marker naming the byte count and line range of the dropped section. Quoting across a marked gap is forbidden by the prompt; the model is instructed to refuse to quote text that crosses the marker.

## Model-written memory treated as fact

**Symptom:** A model generated a memory artifact as part of its reasoning. The system persisted this artifact into the memory store as if it were a verified fact. Subsequent runs retrieved and acted on the fabricated memory.

**Cause:** There was no quarantine step between model generation and persistence. Anything the model wrote was assumed to be authoritative.

**Guard:** A quarantine stage exists between generation and storage. Memory artifacts are marked as unverified, and a separate process reviews them before they are committed to the durable store. The model is not permitted to write directly to memory; it can only propose changes that a separate verification step accepts or rejects.

## General lesson

An automated evaluator that checks only shape will eventually approve confident nonsense at scale. When the stakes of approval are high—especially for things that are expensive to undo—the expensive checks belong on substance, not form. Verify facts out of band, freeze them into static files, and use those files as the source of truth rather than re-computing or re-fetching on each run. This trades one-time verification cost (potentially large) for unlimited confidence in every subsequent use.
