# Refillable-slot orchestrator for the frozen 227-person redo

This is the targeted overlay for `enrichment-orchestrator-prompt.md`. Follow
that complete deterministic workflow, using only:

`TARGET_MANIFEST=age-27-people/backups/2026-08-27-enrichment-redo-227/target-manifest.json`

Pass `--target-manifest "$TARGET_MANIFEST"` to status and the single
`select --all-eligible` command. Use manifest-aware `target_remaining` as the
completion authority and never process an out-of-manifest QID. The run freezes
all currently remaining target QIDs once, assigns stable 100-person approval
tranches, and uses three refillable byte-capped semantic slots. Do not split it
into 60-person cohorts and do not select replacements.

Stage in `/tmp/wikipedia-lede-age_27_people.test.csv` and
`/tmp/wikipedia-lede-age_27_people.test.review.csv`. The helper owns assignment
membership, attempts, exceptions, tranche readiness, reviewed hashes, and
migration membership. Continue later semantic work while earlier tranches wait
for approval. A QID/role receives at most an initial attempt plus two diagnosed
recoveries; exhausted work and `no_adequate_candidate` move to the exception
lane without blocking later target QIDs. Resolved exceptions require catch-up
approval.

For each reviewable tranche, present all non-exception members with name,
Wikipedia link, cause, manner, occupation, QIDs, status, stay overrides, and
exact removal reasons; list exceptions separately. Freeze the review manifest,
pause for explicit approval, record its exact hash, and migrate only through
the approval-bound command. Never write live data directly.

Finish when `scheduler-status` reports `processing_remaining: 0` and
`processing_complete: true`: every frozen target is migrated or explicitly
excepted, all leases are released, all approved migrations verify, and tests
pass. Report external `target_remaining` separately because deliberately
excepted public rows remain eligible there. Report initial/final target counts, tranche sequence,
assignment byte metrics, recoveries, exceptions, statuses, removals,
vocabulary, retrieval metrics, approvals, migrations, wall time, and paths.
