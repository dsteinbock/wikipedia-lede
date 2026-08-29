# Deterministic refillable-slot Wikipedia enrichment workflow

Process one frozen queue and stop. Use `age-27-people/enrichment_batch.py` for
every deterministic action. `--all-eligible` is the standard selection mode;
`--batch-size N` remains available for bounded compatibility runs and is
mutually exclusive with it.

## Boundaries

- Preserve unrelated changes. Do not commit, push, edit Wikidata, use general
  web search, or issue Wikimedia requests outside the helper.
- Stage in `/tmp/wikipedia-lede-age_27_people.test.csv` and
  `/tmp/wikipedia-lede-age_27_people.test.review.csv`. Live files change only
  through an exactly approved migration.
- The controller owns selection, retrieval, packets, assignment packing and
  leases, artifact installation, retries, exceptions, article selection,
  vocabulary, assembly, public writes, browser data, and verification.
- Exactly three semantic assignments may be leased concurrently. Each fresh
  Luna/medium agent performs one manifest's homogeneous role and then exits.
  It receives no network/tools and writes one JSON array containing exactly one
  result per manifest item, in order.

## Queue initialization

Copy live people/review files to the staged paths once. Run status, then:

```sh
python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" select --all-eligible
python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" fetch --cohort RUN_DIR
python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" packetize --cohort RUN_DIR
```

Add `--target-manifest PATH` to status and select for a manifest-bound run. The
selected QIDs, ordinals, and 100-person approval tranches never change.

## Scheduling

Treat `scheduler-status` as the authority. Run its serial control actions
first: `fetch-alternates` fetches alternates for newly reviewed rejected
English pages, and `aggregate-ready` runs `aggregate-eligibility --ready-only`
to unlock downstream work per QID.

For each free slot:

```sh
python age-27-people/enrichment_batch.py claim-assignment --cohort RUN_DIR --slot 1
```

The returned immutable manifest names the role prompt, all input paths, exact
item keys, byte counts, ceilings, and output format. Spawn only that work. Give
the resulting JSON array back through:

```sh
python age-27-people/enrichment_batch.py complete-assignment \
  --cohort RUN_DIR --assignment-id A000001 --input WORKER_OUTPUT
```

For a diagnosed agent failure, use `--failed-reason` instead of `--input`.
Completion releases the slot even when some items fail. Refill immediately.
The helper preserves valid items, rejects extras and invalid items, allows an
initial attempt plus two recoveries, then routes exhausted items to exceptions.
`no_adequate_candidate` is an immediate exception. Independent work continues.

The default packing ceilings are 256 KiB/20 items for both eligibility roles,
160 KiB/15 for death evidence, 64 KiB/20 for death classification, 32 KiB/20
for identity, and 32 KiB/12 labels for vocabulary. These cap serialized input,
which is the calibrated deterministic proxy for the roughly 500k recorded
input-token target; correctness never depends on private runtime token logs.

## Semantic roles

- `article-eligibility.md`: English or alternate article facts only.
- `death-evidence.md`: complete selected-packet death evidence only.
- `death-classification.md`: supplied evidence IDs and canonical labels only.
- `identity.md`: lead and infobox identity only.
- `vocabulary-resolution.md`: supplied novel label and candidates only.

English and alternate eligibility remain separate. Deterministic aggregation
selects English or the largest qualifying alternate and applies only the
approved musician/archive stays for an otherwise sole
`no_dedicated_person_article` reason. Living, nonhuman, age conflict, and
identity mismatch remain possible removals.

## Reviewable tranches

`scheduler-status` reports ready, exception, and reviewable counts for each
stable 100-person tranche. A QID is ready after a possible-removal decision or
all required downstream artifacts. Exceptions count as settled for queue
progress but are excluded from normal approval and listed separately.

For a reviewable tranche N:

1. `assemble --cohort RUN_DIR --tranche N` into its default tranche directory.
2. Run known vocabulary resolution, lookup, assignment-based novel resolution,
   and deterministic vocabulary application for that proposals file.
3. Validate and `apply --tranche N` to the staged people/review paths.
4. Run `verify --tranche N --rebuild-browser --run-tests` against staging.
5. Present the complete summary table and exact exceptions to the user.
6. Run `prepare-tranche-review` with the tranche proposals and staged paths.
   Record the returned review manifest and hash, then pause.
7. If the user requests changes to named items, call `queue-review-correction`
   for each QID and reason. Do not rebuild or re-hash the tranche; those items
   move to the manual correction lane and the unchanged remainder stays accepted.
8. After explicit approval only, call `record-tranche-approval` with the original
   reviewed hash, followed by `migrate --approval APPROVAL_FILE`.
9. Verify live data and the removal ledger. Other semantic slots and later
   staged tranches may continue throughout the approval wait.

The review hash covers the exact QID list, proposals, staged public rows, and
relevant review rows, with per-item fingerprints for partial acceptance.
Unrelated later-tranche changes and edits to queued correction items do not
invalidate accepted items; changes to accepted content do. A resolved exception
or correction requires a catch-up review/approval.

Report queue/tranche counts, active leases, assignment input bytes, attempts,
exceptions, semantic status counts, vocabulary results, retrieval metrics,
tests, approvals, migrations, staged/live paths, and wall time. Finish only
when `scheduler-status` reports `processing_remaining: 0` and
`processing_complete: true`, meaning every frozen QID is migrated or explicitly
in the exception lane and no lease remains.
