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

Fallback labels are mapped to Wikidata vocabulary, alphabetized, semicolon-separated, and positionally aligned with their QIDs. Literal `somevalue` records an article-supported unknown value and therefore has no QID. It is distinct from a blank, which means that fallback research has not supplied a value. The review status is one of `settled`, `provisional`, `disputed`, `unknown`, or `possible_removal`; the last status flags living, nonhuman, age-incompatible, identity-mismatched, or non-biographical-page cases and makes them terminal for enrichment pending stronger review. Every non-settled row is merged into the cumulative stronger-model review CSV for future review or rescanning after Wikidata or Wikipedia changes. In that review CSV, an unknown proposed field is serialized as `somevalue` with a blank QID, while proposal columns for already-effective fields remain blank.

For display, each field independently prefers a usable item-valued Wikidata statement. Absent, Wikidata `somevalue`, and Wikidata `novalue` statements activate the preserved Wikipedia fallback. A fallback remains stored but dormant if Wikidata later gains a usable value. Regeneration preserves all seven columns by Wikidata ID; new people start blank and removed people disappear normally.

The pilot fallback review uses the current English Wikipedia article, with lead sentence, lead paragraph, infobox, and article body as successive sources. A qualifying source must be a page dedicated to the named human individual. If English resolves to an event, case, list, group, or another person, every non-English sitelink is reviewed and the largest qualifying dedicated biography is used. Primary identity or reason-for-notability can populate the occupation-shaped field even when it is not a conventional job.

## Wikipedia enrichment batches

[`enrichment_batch.py`](enrichment_batch.py) handles the deterministic mechanics around LLM review. It freezes the correctly sorted cohort, bulk-fetches redirect-resolved revisions into QID-keyed caches, preserves citation text while creating hierarchical semantic packets, validates structured proposals before public writes, reuses established vocabulary, atomically applies one cohort, merges the cumulative stronger-model queue, and checks browser/musician invariants. It deliberately does not infer cause, manner, or identity.

Use the dependency-capable repository interpreter:

```bash
venv/bin/python age-27-people/enrichment_batch.py status
venv/bin/python age-27-people/enrichment_batch.py select --batch-size 100
venv/bin/python age-27-people/enrichment_batch.py fetch --cohort RUN_DIR
venv/bin/python age-27-people/enrichment_batch.py packetize --cohort RUN_DIR
venv/bin/python age-27-people/enrichment_batch.py init-proposals --cohort RUN_DIR
```

The select command prints `RUN_DIR`. The proposal file is JSONL with one fixed-cohort record per line. A missing fallback is represented by an array of `{"label": "...", "qid": "Q..."}` pairs; an already-effective field remains JSON `null`. After semantic review:

```bash
venv/bin/python age-27-people/enrichment_batch.py resolve-known --proposals RUN_DIR/proposals.jsonl
venv/bin/python age-27-people/enrichment_batch.py lookup-vocabulary --proposals RUN_DIR/proposals.jsonl
venv/bin/python age-27-people/enrichment_batch.py validate --cohort RUN_DIR --proposals RUN_DIR/proposals.jsonl
venv/bin/python age-27-people/enrichment_batch.py apply --cohort RUN_DIR --proposals RUN_DIR/proposals.jsonl
venv/bin/python age-27-people/enrichment_batch.py verify --cohort RUN_DIR --rebuild-browser --run-tests
```

`status` is a read-only effective-eligibility count suitable for the serial orchestrator. `resolve-known` fills mappings established by earlier CSV rows or the persistent ignored vocabulary cache and writes any still-unresolved labels to `RUN_DIR/unresolved_vocabulary.json`. `lookup-vocabulary` serially queries and caches Wikidata Search API candidates for those unique labels without automatically choosing a meaning. Wikimedia requests use a contact-bearing user agent, compression, `maxlag`, bounded retries, and `Retry-After` handling. `validate` is read-only; `apply` refuses to write when the cohort, mappings, source records, proposal arrays, or protected columns are inconsistent.

## Classification

- `confirmed`: every possible best-ranked birth/death combination gives completed age 27.
- `possible`: age 27 occurs and the complete range of possible ages remains within 26 through 28.

Year, month, and day precision are expanded into date bounds using the stated Gregorian or Julian calendar. Coarser or unsupported dates, invalid chronologies, wider age ranges, and ranges that do not contain 27 are excluded rather than guessed.

## Snapshot

The checked-in CSV was generated on **2026-08-25** by a complete live crawl. WDQS returned **4,918** year-difference candidates. Local structured-date classification retained **2,956 people: 1,549 confirmed and 1,407 possible**. It excluded 1,738 candidates outside the strict age rule and 224 with unsupported or otherwise invalid structured dates.

The structured Wikidata output contains 449 people with no best-ranked P106 occupation; those blank values are intentional and do not affect inclusion. Wikipedia fallbacks currently cover only the approved pilot rows.

Wikidata data is available under [CC0](https://www.wikidata.org/wiki/Wikidata:Licensing); source: Wikidata. Completeness is relative to Wikidata statements, English Wikipedia sitelinks, and WDQS availability at generation time. Missing or incorrect dates, human classifications, or sitelinks can cause omissions, and WDQS updates may lag Wikidata edits.

## Tests

```bash
python -m unittest discover -s age-27-people/tests -v
```
