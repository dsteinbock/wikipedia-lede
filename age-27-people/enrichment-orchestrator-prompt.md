# Refillable-slot orchestrator for complete age-27 Wikipedia enrichment

Work in `/Users/daniel/Dropbox/Code/wikipedia-lede`. The checked-in helper is
the scheduling and correctness authority. The orchestrator may spawn only the
assignment manifest returned by `claim-assignment`; it must not choose QIDs,
roles, batch sizes, retries, exceptions, or approval membership itself.

## Fixed execution envelope

- The orchestrating Codex occupies one environment slot and may keep up to six
  fresh semantic subagents active in parallel, using refillable slots numbered
  1–6. Never create an intermediate cohort worker.
- Use fresh Luna/medium agents. Give an agent only its immutable assignment
  manifest, its referenced prompt and inputs, and a unique output path.
- An assignment is homogeneous by role and deterministically capped by both
  serialized-input bytes and item count. A single oversize input is explicitly
  marked and runs alone.
- Semantic agents do not use tools, network access, shared files, or subagents.
  They return one JSON array in manifest order and then finish. The controller
  validates and installs every result separately, preserving valid partial
  output and rejecting duplicates, extras, malformed results, or overwrites.

## Start or resume the live queue

Refresh the two staged files once from live state:

```sh
TEST_OUTPUT_CSV=/tmp/wikipedia-lede-age_27_people.test.csv
TEST_REVIEW_CSV=/tmp/wikipedia-lede-age_27_people.test.review.csv
cp age-27-people/age_27_people.csv "$TEST_OUTPUT_CSV"
cp age-27-people/wikipedia_stronger_model_review.csv "$TEST_REVIEW_CSV"
```

An existing `RUN_DIR` may be resumed only when the user explicitly supplies it.
Never search the cache for an old run and treat its completed state as the state
of the live enrichment queue.

Run `status --limit 0` against the requested scope: use `--target-manifest` only
when the user supplied one; otherwise status is global. If `eligible_count` (or
manifest-scoped `target_remaining`) is nonzero, immediately freeze all of those
currently eligible QIDs with a fresh `select --all-eligible` in the same scope.
Run `fetch` and `packetize` serially. The frozen cohort assigns stable ordinals
and 100-person approval tranches.

## Independent ambiguous-member Wikipedia review lane

Run this lane independently of death-field enrichment. An ambiguous member is
every current live row whose `age_status` is `possible`, plus every live row
with more than one Wikidata-derived `birth_date` or `death_date`. Main-lane
terminal status, migration status, historical tranche membership, exception
state, and partially completed work do not exempt a member. Therefore the
first run backfills all 1,411 currently eligible members, including members
already processed by the existing review system. `age_outside_26_28` remains a
separate main-lane article-eligibility signal and is not folded into this lane.

This lane is report-only. It may write
`age-27-people/wikipedia_ambiguous_members_review.csv`, ignored cache/state, and
frozen review artifacts. It must not change membership, dates, removals, the
main stronger-model review CSV, browser data, or site data.

Start with `ambiguous-status --limit 0`, then freeze every pending member with
`select-ambiguous`. The ignored state ledger skips a member only when its
current QID, name, URL, age status, birth dates, and death dates have the same
fingerprint as its completed review. Use `--rescan-all` only for an intentional
full rereview. Run the ordinary `fetch` and `packetize` commands against the
new `RUN_DIR`, then run `scan-ambiguous`.

The deterministic scan has two narrow purposes:

- Search cleaned biographical prose for explicit age wording at any plausible
  age, and citation `title`/`chapter` text for the narrow 26/27/28 targets.
  Exclude URLs, reference bodies, bibliography sections, publishers, authors,
  and other citation metadata. Preserve a range such as `aged 27–28` as one
  non-singular range rather than two age candidates.
- Extract common explicit birth/death date formats from the labeled infobox and
  complete lead material, including a conventional parenthetical lifespan even
  when a short description or image precedes the actual biographical lead. A
  Wikipedia date is eligible only when its precision is equal to or better than
  the best current Wikidata-derived precision for that field.

A single explicit infobox age—including one deterministically rendered by a
complete `death date and age` template—needs no semantic confirmation. Labeled
infobox dates and role-labeled parenthetical lead lifespans also need no semantic
confirmation. Body and citation-title age hits that still require subject/death
context enter the narrow semantic queue.
Give agents only the candidate excerpts produced by
`ambiguous-role-input`; never send the full article again.

Use `ambiguous-scheduler-status`, `claim-ambiguous-assignment --slot N`, and
`complete-ambiguous-assignment` as a refillable loop. The ambiguous lane shares
the same six numbered Luna/medium slots with the main lane: at most six
semantic assignments may be live across both lanes, and a slot number may not
be leased in both at once. Its byte cap is 64 KiB/20 members. Failed items get
the same initial attempt plus two diagnosed retries, then enter this lane's
separate exception list.

When an ambiguous tranche is `reviewable`, run
`finalize-ambiguous-tranche --tranche N`. This immediately upserts successful
or unresolved evidence hits into the cumulative manual-review CSV and records
no-hit/false-positive completion in ignored state; it has no migration or user
approval step because it cannot mutate public data. Present the generated
Markdown table and report hash. Recommended actions are deterministic:

- `discard` when one singular age-at-death candidate establishes a non-27 age;
- `elevate` when age 27 is established for a current `possible` member;
- `update` when accepted equal-or-better Wikipedia dates improve or reduce the
  date alternatives without changing membership status; and
- `review` only when two or more evidence candidates establish conflicting
  singular ages at death. An explicit singular age statement and a birth/death
  pair that calculates one age each count as candidates; ranges, unresolved
  date alternatives, and ambiguous semantic hits do not; and
- `none` when the evidence establishes no membership or date action.

A confirmed explicit age may select a date alternative only when exactly one
combination remains compatible. An unresolved date alternative does not block
an independently established membership action. Conflicts that do not produce
a second singular age may remain recorded but do not force `review`. After every frozen
ambiguous cohort is finalized, run live
`ambiguous-status --limit 0` again and continue if new or changed members are
pending.

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
5. Continue while other tranches await review or approval. The helper normally
   reserves one upstream eligibility assignment whenever upstream work remains,
   but it gives ready identity work from the earliest still-open approval
   tranche priority so a tranche cannot remain stalled behind the global
   eligibility backlog; other slots continue to favor ready downstream work.
6. `processing_complete: true` means only that this frozen cohort has no more
   schedulable work. It never means the live enrichment queue is complete.

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
and pause for explicit user approval. If `exceptions_excluded` is non-empty,
first delegate the exact exception list (with cached candidate search results)
to a semantic sub-agent for reasonable mapping proposals. Freeze those
proposals with `prepare-exception-review`; it writes a separate Part 2 JSON and
Markdown artifact whose linked-name rows are sorted by problematic field. Show
Part 2 alongside the main table. Approval is blocked until the exception
artifact covers exactly the tranche's excluded QIDs. If the user requests
changes to individual items, call `queue-review-correction` once for each named
QID and reason. Do not regenerate or re-hash the tranche: the named items enter
the manual correction lane and the unchanged remainder keeps its original
review hash. After approval, pass that original hash to
`record-tranche-approval`; it records the accepted QIDs separately from the
corrections. Only then may `migrate --approval APPROVAL_FILE` patch the accepted
QIDs into live data and append their approved removals to the permanent ledger.
Migration checks accepted items independently and ignores later edits to queued
correction items.

Later tranches may keep processing and staging while an earlier tranche awaits
approval because the review hash excludes unrelated rows. Resolved exceptions
and corrections use a later catch-up review and approval. No semantic processing waits on user
approval, but no live write occurs without the exact approved manifest.

## Deterministic ownership

The helper owns queue order, tranche membership, role readiness, byte packing,
six-slot leases, upstream reservation, attempts, exception routing, artifact
validation, article selection, vocabulary persistence, assembly, staging,
review hashes, migration membership, live writes, browser rebuilds, and tests.
The orchestrator owns only agent spawning, returning outputs, displaying review
tables, receiving genuine user approval, and reporting progress.

After a frozen cohort reports `processing_remaining: 0` and
`processing_complete: true`, run live `status --limit 0` again in the original
scope. Compare its remaining count with the cohort's durable exception and
correction QIDs, which remain eligible because they do not mutate public data.
If the remaining count is greater than the number of distinct durable lane QIDs,
immediately create a fresh `select --all-eligible` cohort and continue the same
refillable loop. A count equal to the durable lane count means only manual items
remain.

Declare the overall job complete only when every live eligible item in scope is
already represented by a durable exception or correction entry and there are no
active leases or unapproved reviewable tranches. A historical completed
`RUN_DIR` is never evidence of overall completion. Preserve unrelated dirty work;
do not commit, push, edit Wikidata, or make semantic/network decisions outside
the helper.
