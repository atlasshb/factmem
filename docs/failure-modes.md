# Failure Modes

Seven real failures from a production AI agent system. Each is written as a short case: what was observed, what caused it, and what now prevents it.

---

## 1. A dry-run that persisted its checkpoint

**Symptom.** A dry-run completed without writing any output records, which was expected. The following real run also wrote zero records and reported success.

**Cause.** The state-save function recorded every processed item into the checkpoint store regardless of the dry-run flag. The first real run consulted the checkpoint, found every item already marked as seen, and had nothing left to do.

**Guard.** The state-save function now accepts the dry-run flag and returns early without writing when it is set. A test exercises a dry-run followed immediately by a real run and asserts that the real run produces output.

---

## 2. A cheap model fabricating structured data

**Symptom.** A pipeline stage asked a small model to produce a table of open-source projects with repository names, star counts, and last-updated dates. The output looked correct: right columns, plausible values, no obvious errors.

**Cause.** The model invented every row. An automated evaluator checked column names and value types and passed the result, because the output had the right shape. None of the repositories existed.

**Guard.** Facts of this kind are now verified out of band once and written to a static file that the job reads on subsequent runs. The model is not asked to re-derive them each time.

---

## 3. Rate-limit exhaustion masking as success

**Symptom.** Every row in the output carried an "unknown" value for a particular field. The job reported success and no error was logged.

**Cause.** The pipeline made one external API call per row on every run. An hourly quota was exhausted early in each run; the remaining calls returned rate-limit errors that the code silently converted to the sentinel value "unknown."

**Guard.** The same static file introduced for failure 2 eliminated the per-row API calls entirely. One file read replaces N network calls, and there is no quota to exhaust.

---

## 4. A secret-detection regex that missed a token at a sentence boundary

**Symptom.** A secret token appearing at the end of a sentence passed the detection check undetected. The same token mid-sentence was caught correctly.

**Cause.** The regex ended with a negative lookahead that excluded any following character that was either a word character or a period. A token followed by a full stop did not match because the lookahead rejected the period.

**Guard.** The lookahead now excludes word characters only, leaving punctuation outside its scope. The test suite has an explicit case with the secret token in final position, followed by a period.

---

## 5. Silent truncation of staged context

**Symptom.** A model produced a confident quotation from a document that did not contain it. The quoted passage spanned a section that had been truncated to fit a context limit.

**Cause.** Oversized files were cut at a character limit with no marker at the cut point. The model received a contiguous-looking block of text, inferred that it was complete, and quoted freely across the gap.

**Guard.** Truncation now inserts an explicit marker stating how many characters were dropped and instructing the model not to quote across it. The system prompt reinforces this.

---

## 6. Model-written memory treated as established fact

**Symptom.** A claim introduced by one agent appeared in the outputs of several later agents as a cited fact. The original claim was incorrect.

**Cause.** Anything a model writes to shared memory is readable by every subsequent agent with no indication of its provenance. Later agents read it, cited it, and built further inferences on it.

**Guard.** Model output now enters memory at a low-confidence tier with a tag indicating its source. It is not treated as established fact until a human explicitly promotes it.

---

## 7. An evaluator scoring form rather than substance

**Symptom.** A pipeline stage passed its automated quality check twice. Inspection showed the model had re-summarised its own input rather than performing the assigned task.

**Cause.** The evaluator was a local model that checked whether the output was on-topic and well-formed. The output was both. It did not check whether the task had been done.

**Guard.** Cheap automated checks are now used only to gate cheap things. Any step that is expensive to undo — or where a plausible-looking wrong answer is worse than no answer — requires either a substance check or a human review before the result is used downstream.

---

## General lesson

An automated evaluator that checks shape will eventually approve confident nonsense. The cost of that approval is proportional to how hard the mistake is to reverse: a wrong shape is caught immediately, a wrong fact can propagate for months. Put expensive checks where mistakes are expensive.

It is worth noting that the author of this document also verified a generated file by its line count and shipped an empty one. The failure mode is not specific to machines.
