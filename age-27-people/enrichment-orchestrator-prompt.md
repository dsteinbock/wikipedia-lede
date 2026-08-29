# Refillable-slot orchestrator for complete age-27 Wikipedia enrichment

Work in `/Users/daniel/Dropbox/Code/wikipedia-lede`. The checked-in helper is
the scheduling and correctness authority. The orchestrator may spawn only the
assignment manifest returned by `claim-assignment`; it must not choose QIDs,
roles, batch sizes, retries, exceptions, or approval membership itself.

## Fixed execution envelope

- The orchestrator occupies one environment slot. Exactly three refillable
  semantic slots are available, numbered 1–3. Never create an intermediate
  cohort worker.
- Use fresh Luna/medium agents. Give an agent only its immutable assignment
  manifest, its referenced prompt and inputs, and a unique output path.
- An assignment is homogeneous by role and deterministically capped by both
  serialized-input bytes and item count. A single oversize input is explicitly
  marked and runs alone.
- Semantic agents do not use tools, network access, shared files, or subagents.
  They return one JSON array in manifest order and then finish. The controller
  validates and installs every result separately, preserving valid partial
  output and rejecting duplicates, extras, malformed results, or overwrites.

## Start one unlimited frozen queue

Refresh the two staged files once from live state:

```sh
TEST_OUTPUT_CSV=/tmp/wikipedia-lede-age_27_people.test.csv
TEST_REVIEW_CSV=/tmp/wikipedia-lede-age_27_people.test.review.csv
cp age-27-people/age_27_people.csv "$TEST_OUTPUT_CSV"
cp age-27-people/wikipedia_stronger_model_review.csv "$TEST_REVIEW_CSV"
```

Run manifest-aware `status`, then freeze all currently eligible QIDs with
`select --all-eligible` (plus `--target-manifest` when supplied). Run `fetch`
and `packetize` serially. The frozen manifest assigns stable ordinals and
100-person approval tranches. Newly eligible QIDs belong to a later run.

## Refillable scheduling loop

1. Query `scheduler-status --cohort RUN_DIR`.
2. Perform every returned `control_action` serially:
   `fetch-alternates` owns Wikimedia traffic; `aggregate-ready` means
   `aggregate-eligibility --ready-only`. Query status again afterward.
3. For each free slot, call `claim-assignment --slot N`. If it returns an
   assignment manifest, spawn exactly one fresh semantic agent for it.
4. On success, pass the returned array to `complete-assignment --input`. On an
   agent failure, diagnose it and use `complete-assignment --failed-reason`.
   Either command releases that slot; refill it immediately.
5. Continue while other tranches await review or approval. The helper reserves
   one upstream eligibility assignment whenever upstream work remains and lets
   the other slots favor ready downstream work.

An initial failure receives at most two diagnosed fresh-agent recoveries.
Exhausted QID/role work enters the durable exception lane and does not block
later QIDs or tranches. `no_adequate_candidate` enters the exception lane
immediately. Use `resolve-exception` only after a concrete correction; it
returns the item to its last durable stage and marks it for catch-up approval.

## Tranche review and migration

When `scheduler-status` marks a tranche reviewable, assemble that tranche with
`assemble --tranche N`, resolve and apply vocabulary, validate, and apply to
the staged CSV/review queue with `apply --tranche N`. Rebuild and test the
staged browser state. Present every non-exception member in a table containing
name, Wikipedia link, cause, manner, occupation, QIDs, status, stay overrides,
and exact removal reasons; list excluded exceptions separately.

Run `prepare-tranche-review` to freeze the exact proposals and only that
tranche's staged people/review rows. Present its `review_hash` with the table
and pause for explicit user approval. If the user requests changes to individual
items, call `queue-review-correction` once for each named QID and reason. Do not
regenerate or re-hash the tranche: the named items enter the manual correction
lane and the unchanged remainder keeps its original review hash. After approval,
pass that original hash to `record-tranche-approval`; it records the accepted
QIDs separately from the corrections. Only then may `migrate --approval
APPROVAL_FILE` patch the accepted QIDs into live data and append their approved
removals to the permanent ledger. Migration checks accepted items independently
and ignores later edits to queued correction items.

Later tranches may keep processing and staging while an earlier tranche awaits
approval because the review hash excludes unrelated rows. Resolved exceptions
and corrections use a later catch-up review and approval. No semantic processing waits on user
approval, but no live write occurs without the exact approved manifest.

## Deterministic ownership

The helper owns queue order, tranche membership, role readiness, byte packing,
three-slot leases, upstream reservation, attempts, exception routing, artifact
validation, article selection, vocabulary persistence, assembly, staging,
review hashes, migration membership, live writes, browser rebuilds, and tests.
The orchestrator owns only agent spawning, returning outputs, displaying review
tables, receiving genuine user approval, and reporting progress.

Completion requires `scheduler-status` to report `processing_remaining: 0` and
`processing_complete: true`: every frozen QID is migrated or explicitly in the
exception lane, with no active lease or unapproved reviewable tranche and with
passing tests. External `target_remaining` may still count deliberately
excepted rows because exceptions do not mutate public data. Preserve unrelated dirty
work; do not commit, push, edit Wikidata, or make semantic/network decisions
outside the helper.
