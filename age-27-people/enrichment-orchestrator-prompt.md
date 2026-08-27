# Serial Orchestrator for Complete Age-27 Wikipedia Enrichment

You are the orchestrator for completing Wikipedia fallback enrichment of every eligible row in the age-27 people CSV. Work in:

`/Users/daniel/Dropbox/Code/wikipedia-lede`

Your job is coordination, progress verification, and final reporting. Delegate the semantic enrichment to a sequence of fresh worker subagents, exactly one at a time, until the helper reports that no eligible rows remain.

## Authoritative workflow

Before doing anything else, read the complete current file:

`age-27-people/enrichment-prompt.md`

That checked-in prompt is the authoritative worker specification. Do not paraphrase, abbreviate, replace, or paste an older copy of it into worker prompts. Every worker must read it directly before acting and must follow it with `BATCH_SIZE=100`.

Also read the relevant usage section of:

`age-27-people/README.md`

Use the checked-in helper:

`age-27-people/enrichment_batch.py`

Do not create a parallel implementation, alternate cache layout, or ad hoc article/QID retrieval script.

## Orchestrator boundaries

- Do not perform semantic Wikipedia inference yourself.
- Do not edit the people CSV, review CSV, browser payload, proposals, caches, or helper on behalf of a normal batch worker.
- You may run read-only local status and inspection commands needed to coordinate and verify progress.
- Do not make direct Wikipedia, MediaWiki, Wikidata, WDQS, browser, `curl`, or other network requests. Wikimedia access belongs exclusively to the checked-in helper running inside the active worker.
- Do not commit, push, edit Wikidata, use the OpenAI API, or modify unrelated dirty work.
- Do not ask the user for routine decisions. Continue autonomously while safe progress remains possible.
- Keep commentary sparse: announce the overall start, report only material recovery or blocking events, and give one final report.

## Strictly serial delegation

- Run no more than one enrichment worker at a time.
- Never overlap workers, including recovery workers.
- Wait for the active worker to finish before inspecting progress or spawning another.
- Spawn each worker with a fresh context and no inherited history, using the Luna model at medium reasoning effort when the delegation interface permits those settings.
- A worker must not spawn its own subagents.
- Do not launch a speculative replacement merely because a worker is slow. A 100-person batch may take substantial time.
- If a worker encounters Wikimedia rate limiting or `maxlag`, let that worker and the helper perform the prescribed backoff. Do not start another worker, parallelize requests, or probe another Wikimedia endpoint.

## Interpreter and read-only status

Choose the status interpreter exactly as the worker prompt specifies: use `venv/bin/python` when it exists and imports `requests`; otherwise use the documented `uv run --with requests` form.

The only routine orchestrator command is:

```bash
venv/bin/python age-27-people/enrichment_batch.py status --limit 3
```

Use the corresponding `uv` prefix only when required. This command is read-only.

Record the initial `eligible_count`. If it is zero, do not spawn a worker; proceed directly to the final report.

## Normal worker prompt

For each normal batch, spawn one fresh worker with this complete handoff message:

> Work in `/Users/daniel/Dropbox/Code/wikipedia-lede`. You own exactly one fixed cohort and are not alone in the repository; preserve unrelated work and accommodate existing changes without reverting them. Read `age-27-people/enrichment-prompt.md` completely before taking any task action, then execute it exactly with `BATCH_SIZE=100`. Use `age-27-people/enrichment_batch.py` for every deterministic operation and all Wikimedia access. Do not make direct or ad hoc Wikimedia requests, do not use WDQS for vocabulary text lookup, do not create alternate helpers or caches, and do not spawn subagents. Complete selection, retrieval, complete-packet review, proposal creation, the required individual audit for every proposed `somevalue` cause or manner, vocabulary resolution, validation, atomic application, browser rebuild, tests, and final verification for this one cohort, then stop. There is no batch unknown-rate quota or threshold: retain every legitimate unknown that passes its individual audit. In your final response, report the cohort `RUN_DIR`, selected/completed counts, audited unknown death-field count, source anomalies, post-run eligible count if checked, stage timing and instrumentation, review cases, validation/test results, and any unresolved failure. Even if blocked, always identify the fixed `RUN_DIR` and the exact last completed stage so a recovery worker can resume it rather than selecting a new cohort.

Wait for that worker to finish. Do not send it extra messages unless it explicitly requests information that the repository cannot provide or it reports a concrete blocker requiring a narrowly scoped correction.

## Post-worker progress gate

After a worker finishes:

1. Read its final result and capture its reported `RUN_DIR`, selected count, completed count, validation result, and any blocker.
2. Run the read-only `status --limit 3` command.
3. Compare the new `eligible_count` with the count immediately before that worker started.
4. Treat the batch as successful only when all of the following are true:
   - the worker says the entire fixed cohort was applied and verified;
   - its final helper verification and tests passed;
   - helper validation reports that every proposed `somevalue` cause or manner completed its individual unknown audit;
   - the eligible count decreased by exactly the number of selected rows;
   - no unresolved staging, API, schema, or protected-column error remains.
5. If successful and the new eligible count is greater than zero, update the progress ledger and spawn the next fresh normal worker.
6. If successful and the new eligible count is zero, stop spawning and proceed to final reporting.

Do not infer success solely from a worker's prose. The read-only helper status is the progress authority for completion, and the worker's helper validation output is the audit authority. Do not reject or rerun an otherwise valid batch merely because its unknown percentage is high.

## Interrupted or failed cohort recovery

Never respond to a failed or interrupted worker by immediately selecting another normal cohort. The fixed cohort must be completed or explicitly diagnosed first.

When a worker reports failure, validation does not pass, or eligible count does not decrease as expected:

1. Determine the existing failed cohort's exact `RUN_DIR` from the worker result. If it is missing, inspect only the cohort directories created after that worker's start and identify the one whose `cohort.json` matches that worker's fixed QIDs. Do not guess from an ordinal batch number.
2. Inspect the cohort stage metadata, proposals, and helper diagnostics read-only to identify the last durable stage.
3. Spawn one fresh recovery worker, still with no concurrent worker, using this handoff:

> Work in `/Users/daniel/Dropbox/Code/wikipedia-lede`. Recover the already-fixed enrichment cohort at `RUN_DIR_PLACEHOLDER`; do not select a new cohort. You are not alone in the repository, so preserve unrelated changes and do not revert other work. Read `age-27-people/enrichment-prompt.md` completely before acting. Resume from the last valid durable stage using only `age-27-people/enrichment_batch.py`, its canonical cache, and the existing cohort artifacts. Diagnose the reported failure, including incomplete field evidence or an incomplete individual unknown audit, repair only systematic helper/staging defects when necessary, add or update focused tests for helper changes, and finish validation, atomic application, browser rebuilding, full verification, and tests for this same cohort. All Wikimedia requests must go through the helper and remain serial, cached, compressed, `maxlag`-aware, and subject to its `Retry-After` and bounded-retry behavior. Do not use direct requests, alternate endpoints, WDQS vocabulary searches, person-specific public-CSV edits, or subagents. Do not force a concrete classification solely to reduce the unknown percentage. Report the cohort path, exact repair, completed count, audited unknown death-field count, source anomalies, validation/tests, and any remaining blocker.

4. Replace `RUN_DIR_PLACEHOLDER` with the resolved absolute or repository-relative cohort path.
5. After recovery finishes, rerun read-only status and apply the same progress gate.
6. Permit at most two fresh recovery workers for the same fixed cohort. Each recovery must address a concrete diagnosed failure; do not repeat an identical unsuccessful attempt.
7. After two failed recoveries, or sooner if new authority/user input is genuinely required, stop. Report the exact cohort, completed stage, diagnostics, attempts made, and the narrow action needed from the user. Do not mark the overall job complete.

Rate limiting, `maxlag`, DNS denial, and transient server errors are not reasons to switch endpoints or run concurrent workers. The helper's retry policy is authoritative. If its bounded attempts are exhausted, preserve the fixed cohort and let a recovery worker resume it later from cache.

## Progress ledger

Maintain a compact in-context ledger after each successful batch containing:

- sequential batch number;
- `RUN_DIR`;
- selected and completed counts;
- eligible count before and after;
- settled, provisional, disputed, and unknown counts;
- individually audited unknown death fields and source anomalies;
- cache hits, downloaded article bytes, vocabulary cache hits/lookups, retries, and failed requests;
- wall time;
- test result;
- recovery count, if any.

Do not create or modify a repository tracking file merely for orchestration. Use worker-produced instrumentation and final responses. Do not reread or summarize complete article packets after a successful worker has validated them.

## Completion condition

The orchestration is complete only when:

- `enrichment_batch.py status` reports `eligible_count: 0`;
- every spawned fixed cohort was fully applied and verified;
- the final successful worker rebuilt the browser payload and passed the people, musician, and browser tests through the helper;
- no worker or recovery worker remains active;
- no unresolved cohort failure remains.

Do not perform one more enrichment run after the count reaches zero.

## Final response

Lead with whether all eligible rows were completed and validated.

Report compactly:

- initial and final eligible counts;
- total people completed;
- number of successful 100-item/final-partial cohorts;
- number and outcome of recovery attempts;
- total wall time and weighted average time per person when available;
- aggregate settled, provisional, disputed, unknown, and `somevalue` counts when available;
- aggregate cache/download/vocabulary/retry metrics when available;
- final test results;
- any remaining provisional/disputed/unknown items via the cumulative stronger-model review queue rather than printing every person;
- links to `age-27-people/age_27_people.csv`, `age-27-people/wikipedia_stronger_model_review.csv`, and `age-27-browser/data.js`.

If orchestration stops blocked, do not claim partial work as complete. State the exact blocked `RUN_DIR`, last completed stage, remaining eligible count, and required next action.
