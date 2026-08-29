# Age-27 people dataset

This project produces a Wikidata-defined list of all humans who have an English Wikipedia article and definitely or narrowly possibly died at age 27. Occupation never determines inclusion. The census and age classification remain structured-data-only; a small set of descriptive fallback fields can be researched from Wikipedia without changing membership.

## Generate the dataset

From the repository root:

```bash
python age-27-people/generate_csv.py
```

The complete run queries Wikidata serially, caches every successful death-year shard and enrichment batch under the ignored `age-27-people/.cache/` directory, and resumes from that cache after interruption. The final artifact is [`age_27_people.csv`](age_27_people.csv), sorted by earliest possible death date ascending.

Candidate discovery uses bounded Wikidata Query Service ranges. Name, English Wikipedia sitelink, direct occupation labels, cause of death, and manner of death are fetched in batches of 50 through Wikibase GraphQL.

The CSV distinguishes Wikidata's specific [`cause of death` (`P509`)](https://www.wikidata.org/wiki/Property:P509) from its broad [`manner of death` (`P1196`)](https://www.wikidata.org/wiki/Property:P1196). Multiple best-ranked values are alphabetized and separated by semicolons. A blank value means Wikidata has no usable best-ranked item statement for that field; it is not an inference that the cause or manner is unknown in other sources.

Seven additional columns hold hand-reviewed Wikipedia fallbacks:

- `wikipedia_cause_of_death` and `wikipedia_cause_of_death_qids`
- `wikipedia_manner_of_death` and `wikipedia_manner_of_death_qids`
- `wikipedia_occupations` and `wikipedia_occupation_qids`
- `wikipedia_death_review_status`

Fallback labels are mapped to Wikidata vocabulary, alphabetized, semicolon-separated, and positionally aligned with their QIDs. Literal `somevalue` records an article-supported unknown value and therefore has no QID. It is distinct from a blank, which means that fallback research has not supplied a value. The current semantic classifier emits `settled` or `unknown`; `possible_removal` separately flags living, nonhuman, age-incompatible, identity-mismatched, or non-biographical-page cases. Possible removals remain visible in the staged review until approval, then the `migrate` command removes them from the live people CSV and review queue and appends the complete row plus exact reason to the permanent [`removed_entries.csv`](removed_entries.csv) ledger. Future generation and cohort selection exclude every QID in that ledger. Legacy `provisional` and `disputed` values remain schema-valid while the redo replaces them, but new semantic artifacts cannot emit them. Every non-settled row is merged into the cumulative stronger-model review CSV for future review or rescanning after Wikidata or Wikipedia changes. In that review CSV, an unknown proposed field is serialized as `somevalue` with a blank QID, while proposal columns for already-effective fields remain blank.

For display, each field independently prefers a usable item-valued Wikidata statement. Absent, Wikidata `somevalue`, and Wikidata `novalue` statements activate the preserved Wikipedia fallback. A fallback remains stored but dormant if Wikidata later gains a usable value. Regeneration preserves all seven columns by Wikidata ID; QIDs in `removed_entries.csv` remain excluded from regenerated and newly selected data.

The pilot fallback review uses the current English Wikipedia article, with lead sentence, lead paragraph, infobox, and article body as successive sources. A qualifying source must be a page dedicated to the named human individual. If English resolves to an event, case, list, group, or another person, every non-English sitelink is reviewed and the largest qualifying dedicated biography is used. Primary identity or reason-for-notability can populate the occupation-shaped field even when it is not a conventional job.

## Wikipedia enrichment batches

[`enrichment_batch.py`](enrichment_batch.py) is the deterministic control plane around narrowly scoped semantic review. The standard `--all-eligible` mode freezes the complete current queue once, gives every QID a stable ordinal and 100-person approval tranche, performs serial Wikimedia retrieval, and deterministically governs role readiness, assignment packing, leases, retries, exceptions, article selection, vocabulary, staging, reviewed hashes, and migration membership. Bounded `--batch-size N` selection remains available for compatibility. The helper deliberately does not infer eligibility, death evidence, cause, manner, identity, or novel vocabulary meaning.

Exactly three refillable Luna/medium semantic slots run beside the orchestrator. `claim-assignment` returns one immutable homogeneous assignment capped by serialized-input bytes and item count; the agent processes every referenced input, returns one JSON array, and exits. `complete-assignment` validates each result independently, preserves valid partial output, rejects out-of-assignment or malformed results, and immediately releases the slot. One upstream eligibility assignment is reserved while upstream work remains; other slots favor ready downstream work. An initial failure receives at most two diagnosed recoveries before the item enters the exception lane. Exceptions do not block later QIDs or tranches.

The workflow stages in `/tmp/wikipedia-lede-age_27_people.test.csv` and `/tmp/wikipedia-lede-age_27_people.test.review.csv`. A reviewable tranche is assembled, vocabulary-resolved, validated, applied to staging, rebuilt, tested, and presented in full. `prepare-tranche-review` hashes the exact proposals and only that tranche's staged people/review rows, including per-item fingerprints. Later tranches may continue processing and staging while approval is pending. If review identifies an item needing work, `queue-review-correction` moves only that QID to the manual correction lane; the unchanged remainder keeps the original review hash and can migrate immediately after approval. Approved removals are appended to `removed_entries.csv`. Resolved exceptions and corrections require catch-up approval.

Use the dependency-capable repository interpreter:

```bash
TEST_OUTPUT_CSV=/tmp/wikipedia-lede-age_27_people.test.csv
TEST_REVIEW_CSV=/tmp/wikipedia-lede-age_27_people.test.review.csv
cp age-27-people/age_27_people.csv "$TEST_OUTPUT_CSV"
cp age-27-people/wikipedia_stronger_model_review.csv "$TEST_REVIEW_CSV"
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" status
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" select --all-eligible
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" fetch --cohort RUN_DIR
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" packetize --cohort RUN_DIR
venv/bin/python age-27-people/enrichment_batch.py scheduler-status --cohort RUN_DIR
venv/bin/python age-27-people/enrichment_batch.py claim-assignment --cohort RUN_DIR --slot 1
# Give the returned manifest to one fresh agent, then return its JSON array:
venv/bin/python age-27-people/enrichment_batch.py complete-assignment --cohort RUN_DIR --assignment-id A000001 --input WORKER_OUTPUT
```

The select command prints `RUN_DIR`. Follow `scheduler-status` control actions and refill every released slot:

```bash
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" fetch-alternates --cohort RUN_DIR
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" aggregate-eligibility --cohort RUN_DIR --ready-only
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" assemble --cohort RUN_DIR --tranche N
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" resolve-known --proposals RUN_DIR/tranches/NNN/proposals.jsonl
# Run lookup-vocabulary and vocabulary assignments only when unresolved labels exist.
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" validate --cohort RUN_DIR --tranche N --proposals RUN_DIR/tranches/NNN/proposals.jsonl
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" apply --cohort RUN_DIR --tranche N --proposals RUN_DIR/tranches/NNN/proposals.jsonl --review-csv "$TEST_REVIEW_CSV"
venv/bin/python age-27-people/enrichment_batch.py --people-csv "$TEST_OUTPUT_CSV" verify --cohort RUN_DIR --tranche N --review-csv "$TEST_REVIEW_CSV" --rebuild-browser --run-tests
venv/bin/python age-27-people/enrichment_batch.py prepare-tranche-review --cohort RUN_DIR --tranche N \
  --proposals RUN_DIR/tranches/NNN/proposals.jsonl --staged-people-csv "$TEST_OUTPUT_CSV" --staged-review-csv "$TEST_REVIEW_CSV"
# For each item the user sends to manual correction; do not prepare the tranche again:
venv/bin/python age-27-people/enrichment_batch.py queue-review-correction --cohort RUN_DIR \
  --review-manifest REVIEW_MANIFEST --qid QID --reason "USER_REQUESTED_CHANGE"
# Pause for explicit approval of REVIEW_HASH, then record it exactly.
venv/bin/python age-27-people/enrichment_batch.py record-tranche-approval --review-manifest REVIEW_MANIFEST --reviewed-hash REVIEW_HASH
venv/bin/python age-27-people/enrichment_batch.py migrate --cohort RUN_DIR --approval APPROVAL_FILE \
  --proposals RUN_DIR/tranches/NNN/proposals.jsonl --staged-people-csv "$TEST_OUTPUT_CSV" --staged-review-csv "$TEST_REVIEW_CSV"
venv/bin/python age-27-people/enrichment_batch.py verify --cohort RUN_DIR --allow-removed \
  --removed-csv age-27-people/removed_entries.csv --rebuild-browser --run-tests
```

Semantic artifacts remain under `RUN_DIR/semantic/{eligibility,alternate-eligibility,death-evidence,death-classification,identity,vocabulary}/`. Assignment manifests and leases live under `RUN_DIR/scheduler/`; agents never write final semantic paths. The deterministic byte ceilings are 256 KiB/20 items for each eligibility role, 160 KiB/15 for death evidence, 64 KiB/20 for death classification, 32 KiB/20 for identity, and 32 KiB/12 for vocabulary. A single oversize input runs alone. Serialized bytes are a calibrated proxy for the roughly 500k recorded input-token target; correctness does not rely on private runtime token logs.

Article eligibility has two explicit stays that override only the
`no_dedicated_person_article` removal reason. A selected QID is retained when
it is present in the materialized `age-27-musicians/age_27_musicians.csv`
generated from the approved musician hierarchy roots (`Q639669`, `Q1294626`,
`Q822146`, and `Q1198887`), or when its final English article URL after
redirects is one of the person links in
`age-27-musicians/purported-27-club-members.html`. Living, nonhuman,
age-conflicting, and subject-identity-mismatch findings are not overridden.
The proposal records the applied rule in `stay_overrides` for manual review.

`resolve-known` uses the tracked field-specific [`wikipedia_fallback_vocabulary.json`](wikipedia_fallback_vocabulary.json) and the ignored approved-vocabulary cache. It resolves only exact case/whitespace-normalized `(field, label)` mappings and writes structured unresolved records. `lookup-vocabulary` fetches candidates serially for those exceptions. Install each narrow semantic decision with `record-vocabulary`, then use `apply-vocabulary` to update proposals and persist approved mappings serially. Fuzzy or programmed synonym inference is not used. A missing or inadequate QID blocks application and never becomes `somevalue`.

Death evidence is extracted semantically once and assigned stable evidence IDs. The classifier consumes that evidence bundle and references those IDs rather than copying excerpts. Any possible account mentioned in the article—including speculative, suspected, reported, probable, pending, inferred, or competing accounts—is classified as `settled`. Manner may be inferred directly from cause or circumstances without separate explicit evidence. Competing accounts collapse to the project-preferred account rather than `disputed`. Overall `unknown` is reserved for cases where neither cause nor manner has any possible account, or both are explicitly unknown or undisclosed without a theory; one concrete field makes the record `settled`. Identity review sees only the lead sentence, rest of the first lead paragraph, remaining lead section, and infobox. There is no duplicate semantic-verifier pass. `validate` is read-only; `apply` refuses to write when the cohort, mappings, semantic artifacts, source records, proposal arrays, or protected columns are inconsistent.

## Classification

- `confirmed`: every possible best-ranked birth/death combination gives completed age 27.
- `possible`: age 27 occurs and the complete range of possible ages remains within 26 through 28.

Year, month, and day precision are expanded into date bounds using the stated Gregorian or Julian calendar. Coarser or unsupported dates, invalid chronologies, wider age ranges, and ranges that do not contain 27 are excluded rather than guessed.

## Snapshot

The checked-in CSV was generated on **2026-08-25** by a complete live crawl. WDQS returned **4,918** year-difference candidates. Local structured-date classification retained **2,956 people: 1,549 confirmed and 1,407 possible**. It excluded 1,738 candidates outside the strict age rule and 224 with unsupported or otherwise invalid structured dates. The current enriched live file contains **2,921 people** after 35 approved `possible_removal` entries were moved to the permanent exclusion ledger.

The structured Wikidata output contains 449 people with no best-ranked P106 occupation; those blank values are intentional and do not affect inclusion. Wikipedia fallbacks currently cover only the approved pilot rows.

Wikidata data is available under [CC0](https://www.wikidata.org/wiki/Wikidata:Licensing); source: Wikidata. Completeness is relative to Wikidata statements, English Wikipedia sitelinks, and WDQS availability at generation time. Missing or incorrect dates, human classifications, or sitelinks can cause omissions, and WDQS updates may lag Wikidata edits.

## Tests

```bash
python -m unittest discover -s age-27-people/tests -v
```
