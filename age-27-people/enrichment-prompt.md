Continue the Wikipedia fallback enrichment of the age-27 dataset in this repository.

MODEL/EXECUTION CONSTRAINTS

- Work autonomously on the next batch of eligible people, then stop. Use a caller-supplied `BATCH_SIZE`; otherwise default to 100. If fewer eligible rows remain, process all remaining rows. When the caller supplies `TARGET_MANIFEST`, pass it to every `status` and `select` command and process only its remaining QIDs.
- Do not spawn subagents.
- Minimize agent turns, tool calls, and commentary. Give one brief starting update, one midpoint update, and a final report.
- Preserve unrelated dirty work. Do not commit, push, edit Wikidata, or use the OpenAI API.
- The public MediaWiki and Wikidata HTTP APIs are authorized for read-only article retrieval, metadata, and vocabulary/QID lookup. Do not use external sources or general web search.
- Use one canonical ignored workspace: `age-27-people/.cache/wikipedia-fallback/`. Reuse and repair compatible cached tooling instead of creating another implementation or an underscore-named parallel cache.
- Use the checked-in `age-27-people/enrichment_batch.py` for cohort selection, article retrieval, packetization, proposal initialization/validation, known-vocabulary reuse, atomic application, cumulative review merging, browser rebuilding, and invariant tests. Do not reimplement those mechanics in ad hoc inline Python. The helper intentionally does not make semantic judgments.
- If the helper exposes a systematic defect, patch it and its tests before continuing. Do not work around it with person-specific public-CSV edits.
- Fetch and process articles in bulk. Never open articles one at a time in a browser and never print complete articles or the whole 100-person research corpus into tool output.
- Prefer a small number of consolidated operations. Target no more than 25 shell/tool calls and no more than one public-CSV rewrite unless a failed validation requires a repair.

COHORT SELECTION

Read `age-27-people/age_27_people.csv`.

A field is effectively filled when:

1. A usable item-valued Wikidata-derived value exists in `cause_of_death`, `manner_of_death`, or `occupations`; otherwise
2. Its corresponding `wikipedia_*` fallback is nonblank, including the literal `somevalue`.

Wikidata absence and literal `somevalue` or `novalue` are not usable values. A blank Wikipedia fallback is not an error when that field already has a usable Wikidata value.

Select rows having at least one effectively blank cause, manner, or occupation/identity. Exclude rows already fully effective and rows whose persisted review status is `possible_removal`.

Sort eligible rows by most recent possible death date descending, then `wikidata_id` ascending. Parse every semicolon-separated death-date alternative as an ISO-like signed year with optional month and day. For the latest-possible sort key, fill missing month/day with 12/31 and take the maximum alternative. Do not sort by converting the entire date string to `int` or by extracting only an ad hoc substring.

Select `min(BATCH_SIZE, eligible_count)` rows. Before any article retrieval, save the fixed ordered QID list, the selection timestamp, and a snapshot/hash of every protected non-fallback column for those rows under a cohort-specific directory. Never use ordinal filenames such as `batch_000.json` as cross-cohort cache keys.

REQUIRED TWO-PHASE WORKFLOW

Use this order. Do not interleave public CSV rewrites with unfinished research or vocabulary lookup.

1. Record the run start time before repository inspection, selection, or network work.
2. Choose one interpreter: use `venv/bin/python` when it exists and imports `requests`; otherwise prefix the commands below with `UV_CACHE_DIR=/tmp/wikipedia-lede-uv-cache uv run --with requests python`.
3. Run `enrichment_batch.py select --batch-size BATCH_SIZE` and capture the printed cohort directory as `RUN_DIR`. When `TARGET_MANIFEST` is supplied, add `--target-manifest TARGET_MANIFEST`. This freezes the QIDs and protected-column snapshot.
4. Run `enrichment_batch.py fetch --cohort RUN_DIR`, then `enrichment_batch.py packetize --cohort RUN_DIR`.
5. Run `enrichment_batch.py init-proposals --cohort RUN_DIR`. Review every English packet for article eligibility before death/identity inference. For each English page that is not a dedicated person article, record its completed primary review in the proposal, then run `enrichment_batch.py fetch-alternates --cohort RUN_DIR --proposals RUN_DIR/proposals.jsonl` once for the cohort. Review and disposition every returned non-English packet before selecting an article or proposing `possible_removal`. Then perform Wikipedia inference from the selected packets in bounded chunks of 10–20 people and fill `RUN_DIR/proposals.jsonl`. Save each completed chunk immediately; do not repeatedly reread settled chunks. Before assigning `somevalue` to a death field, perform the individual unknown audit specified below.
6. Express each proposed field as an array of `{"label": "...", "qid": "..."}` objects. Use JSON `null` for a field the proposal template marks as already effective. For every needed field, fill its structured `evidence_basis` object with `source_tier`, `section`, an exact packet `excerpt`, and a concise `reason`; use `source_tier: "none"` and a blank section/excerpt only for a genuine `somevalue`. Do not use semicolon-encoded intermediate strings.
7. Run `enrichment_batch.py resolve-known --proposals RUN_DIR/proposals.jsonl`, then `enrichment_batch.py lookup-vocabulary --proposals RUN_DIR/proposals.jsonl`. For labels still unresolved, choose only among the checked canonical labels/descriptions in `RUN_DIR/vocabulary_candidates.json`, add the selected QIDs to the proposals, and rerun `resolve-known` so they persist for later batches. Do not perform ad hoc WDQS or entity-download lookups.
8. Run `enrichment_batch.py validate --cohort RUN_DIR --proposals RUN_DIR/proposals.jsonl`. Only after it passes, run `enrichment_batch.py apply --cohort RUN_DIR --proposals RUN_DIR/proposals.jsonl`.
9. Run `enrichment_batch.py verify --cohort RUN_DIR --rebuild-browser --run-tests`. Do not call the builder or full test suites separately unless this command identifies a defect.
10. Record the end time after verification finishes.

If a staging failure exposes a systematic bug, fix the staging data or helper and rerun the validator. Do not patch individual public CSV rows piecemeal unless the staged proposal itself is correct and only the atomic apply step is defective.

ARTICLE RETRIEVAL AND CACHE RULES

- All Wikimedia access must go through the checked-in helper. Its descriptive `User-Agent` includes project and contact URLs; do not imitate a browser User-Agent or issue direct `curl`, browser, or inline-script requests.
- The helper uses serial GET requests, JSON, `gzip, deflate`, `maxlag=5`, and pipe-separated multi-title batches of 20 for article retrieval. Do not parallelize Wikimedia requests or reduce batching to one request per person.
- Include `redirects=1`, `converttitles=1`, and `formatversion=2` when retrieving pages through the MediaWiki Action API.
- Resolve normalized titles and redirects before matching returned pages to people. Verify every cached record's QID, language, resolved title, revision ID, and nonempty content before reuse.
- Cache per-person article records by QID and language/revision metadata. A batch response may be cached within its cohort directory, but it must never be reused merely because it has the same ordinal position as a later cohort.
- If sandboxed network access fails once with DNS or connection denial, retry the authorized read-only bulk operation through the environment's approval mechanism. Do not repeat equivalent sandbox probes.
- For transient 429, 5xx, timeout, or API-level `maxlag` responses, let the helper honor `Retry-After` and retry at most twice with bounded backoff. A second rate limit is a batch error: stop rather than probing equivalent Wikimedia endpoints.
- Prefer a qualifying dedicated English person article. An English redirect or direct sitelink to an event, case, list, group, or another person is not qualifying. When English is not a dedicated person article, use the helper to retrieve every available non-English Wikipedia sitelink and review all of them. If one or more are dedicated person articles, select the largest qualifying article by API byte size; otherwise propose `possible_removal`. Record the chosen language in the research log, not the public CSV, and never replace the public English `wikipedia_url`.
- A missing or empty page caused by title normalization, redirect handling, network failure, or cache mismatch is a retrieval error, not evidence that the cause, manner, or identity is unknown. Resolve retrieval before inference.

ARTICLE ELIGIBILITY AND POSSIBLE REMOVAL

Complete the structured `article_eligibility` object for every person. Review the primary English packet and, whenever it is not a dedicated person article, every non-English alternate packet returned by the helper. Classify each page as `person`, `event`, `case`, `list`, `group`, or `other`; record whether its subject is human, deceased/living/conflicting/unclear, and age-compatible/outside/conflicting/unknown, with a concise reason.

A qualifying page is dedicated primarily to the named human individual. A name redirect alone is insufficient when its target is about an event, murder/death case, hostage or other list, group, or another person. A dedicated biography in any Wikipedia language qualifies and redeems a non-person English target. Parenthetical disambiguation, translated names, and redirects to another dedicated biography title are allowed.

Set status `possible_removal` when the complete article review supports one or more of these reasons: `living`, `nonhuman`, `age_outside_26_28`, `no_dedicated_person_article`, or `subject_identity_mismatch`. An explicit age outside 26–28, dates whose possible-age interval is disjoint from 26–28, or materially conflicting article ages supports age review; an uncertain interval that still overlaps 26–28 does not. `possible_removal` overrides every death-review status. Set all three proposal fields and their evidence/unknown-review entries to JSON `null`; the helper will preserve any existing fallback values, write only the status, queue the case, and treat it as terminal pending stronger review.

SOURCE AND SEMANTIC-PACKET RULES

For each selected person, use only the complete content of the chosen current Wikipedia article:

- lead and prose
- infoboxes
- tables and captions
- citation titles and visible citation text

Do not follow external citation links. Remove URLs and nonsemantic markup, but retain the visible citation title/text inside `<ref>` tags and citation templates. Do not delete an entire reference merely because it is wrapped in reference markup.

Create a locally stored semantic packet with distinct tiers for:

1. lead sentence
2. rest of lead paragraph
3. infobox
4. rest of article, including tables, captions, and retained citation text

Also retain the article body's heading structure in `article_sections` and provide stable, subject-oriented `death_evidence_candidates` as a review index. The candidate list is neither exhaustive nor a classifier: read the complete four-tier packet, and do not treat a match as being about the subject without checking its context.

Remove navigation, formatting, duplicate reference metadata, maintenance boilerplate, and other nonsemantic markup. Do not truncate the rest-of-article section before inference. Preserve prose after self-closing named references such as `<ref name="source"/>`; such a reference must not consume text through a later closing tag. Regex or keyword matching must not make the final semantic classification without an LLM review of the relevant complete packet.

FIELD PRECEDENCE

Apply this hierarchy independently for each Wikipedia-derived field:

1. Lead sentence
2. Rest of lead paragraph
3. Infobox
4. Rest of article

Higher-priority article text governs when sources conflict. A usable item-valued Wikidata field remains authoritative and must not be replaced; leave its corresponding fallback dormant/blank unless it was already populated by an earlier run.

CAUSE AND MANNER RULES

- Never leave an effectively missing cause or manner blank.
- Extract or infer the most specific article-supported cause. Preserve multiple causal layers when supported, such as an initiating event and resulting fatal condition.
- Derive manner from the selected cause and circumstances in the same proposal pass. If no manner can be supported, use `somevalue`; never defer manner completion to a later repair pass.
- Read circumstances semantically rather than requiring a literal cause phrase. For example, losing control of a motorcycle, striking a pole, and dying at the scene supports a traffic-collision cause and accidental manner. A terminal disease described in a dedicated death/biography passage can support that disease as cause when the passage connects the illness and death and supplies no competing account; mark a conclusion `provisional` when that connection is necessarily inferred rather than directly stated.
- Require subject attribution. Image filenames, navigation/list entries, quotations, and deaths or injuries of relatives, victims, or other named people do not establish the subject's cause or manner.
- Map values to canonical Wikidata labels and QIDs appropriate for P509 and P1196.
- When the successfully retrieved complete article gives no usable basis, store literal `somevalue` with a blank QID. If it explicitly says the cause is unknown, unreported, pending, or unconfirmed, use `somevalue` rather than diagnosing from unrelated history.
- A vocabulary lookup failure is not evidence of an unknown death. Never replace an article-supported label with `somevalue` merely because its QID lookup failed; treat unresolved mapping as a staging error and resolve it before applying the cohort.
- In disputed cases, follow the established project rule: prefer the more inflammatory, dramatic, sensational, or popular account supported by the article. Prefer homicide over suicide when both are presented, and an overdose explanation over suicide or illness when those competing explanations are article-supported.
- Mark materially competing accounts as `disputed`, even after selecting the preferred account.

INDIVIDUAL UNKNOWN AUDIT

There is no target, quota, or batch-percentage threshold for `unknown`. A high final unknown rate is acceptable only when every individual `somevalue` cause or manner passes this audit; a low rate never excuses skipping it.

Immediately before assigning `somevalue` to cause or manner, reread all four complete source tiers in precedence order specifically for that field. Then fill that field's `unknown_review` object with exactly:

- `reviewed_source_tiers`: `["lead_sentence", "rest_of_lead_paragraph", "infobox", "rest_of_article"]`
- `candidate_dispositions`: one entry for every `death_evidence_candidates` ID, each containing a `disposition` and nonblank field-specific `reason`
- `conclusion`: a concise explanation of why the complete article still cannot establish that field

Allowed dispositions are `not_about_subject`, `does_not_establish_field`, `explicitly_unknown`, and `unconfirmed_without_usable_account`. Audit every candidate separately for cause and manner because a passage may establish one but not the other. An empty candidate list still requires the complete four-tier reread and conclusion. If any candidate or other packet passage supports a value, do not use `somevalue`; create the supported proposal and classify it as settled, provisional, or disputed as appropriate.

Record article/data inconsistencies in `source_anomalies`, including unresolved redirects, an article depicting the subject as living, mismatched age or identity, list/article contamination, or evidence about another person. Do not change protected Wikidata-derived or population fields to repair such anomalies. An unresolved redirect or unusable retrieval blocks validation and is not a legitimate unknown.

OCCUPATION/IDENTITY RULES

- Never leave an effectively missing occupation/identity blank.
- This field represents the article's primary identity or reason for notability, not merely paid employment. Reason for notability normally outranks incidental occupation; for example, prefer mass shooter over bus driver.
- Keep multiple identities when the highest-priority text presents them as equally central.
- Map each identity to the closest canonical Wikidata label and QID suitable for the P106-shaped fallback columns, even when it would not conventionally be submitted as P106.
- Dwarfism, diseases, disabilities, physical characteristics, ethnicity, nationality, and similar attributes are not identities or types of person. `record holder` is an identity; dwarfism is not.
- If the successfully retrieved complete article genuinely supplies no usable identity descriptor, store `somevalue` with no QID.
- As with death fields, QID lookup failure must not be converted to `somevalue`.

STATUS RULES

Set `wikipedia_death_review_status` from the effective death account:

- `possible_removal`: article eligibility raises a living, nonhuman, age, dedicated-page, or identity concern; this overrides every status below
- `disputed`: the article presents materially competing accounts
- `provisional`: the selected account is reported, probable, likely, suspected, or necessarily inferred, without a material competing account
- `unknown`: effective cause or manner remains `somevalue` because the complete article supplies no usable account
- `settled`: the article gives a direct, internally consistent account

Apply that priority order. An identity-only `somevalue` does not make an otherwise settled death account `unknown`. For occupation-only enrichment where existing Wikidata death fields are usable, use `settled` unless the article reveals a material death-account dispute or uncertainty.

QID NORMALIZATION

Resolve vocabulary once, after all proposals are drafted:

1. Build a label-to-QID map from every aligned Wikipedia fallback pair already present in the current CSV, not a historical fixed number of rows.
2. Load and reuse `age-27-people/.cache/wikipedia-fallback/vocabulary.json` when present, checking for conflicts with current CSV mappings.
3. Extract and deduplicate all still-unresolved proposed labels for the cohort.
4. Run the helper's `lookup-vocabulary` command once. It queries Wikidata's `wbsearchentities` Search API serially at most once per remaining unique label and caches the candidate label/description lists. Search is the appropriate endpoint for label text; do not use WDQS for fuzzy/text vocabulary lookup.
5. Check each candidate's canonical English label and description before selecting its QID. Do not use `Special:EntityData` or another full-entity download merely to search for a concept; such access is appropriate only after an entity ID is already known and its full entity data is actually needed.
6. Persist successful mappings for later batches. Do not repeatedly search the same label during one run or issue one Wikidata request per person.
7. Resolve conflicts consistently across the cohort. Validate that every non-`somevalue` label has exactly one aligned QID.

Do not use a broad or unrelated first search result without checking its canonical English label and description. Do not downgrade a valid semantic label because a lookup endpoint was rate-limited.

STAGING VALIDATION

Before touching public artifacts, require all of the following:

- exactly one proposal for every fixed-cohort QID and no proposal outside the cohort
- the retrieved article record is nonempty and contains QID, article URL, language, resolved title, revision ID, and byte size
- every effectively missing field has a nonempty proposed array
- `somevalue` is the sole value in its array and has a blank QID
- every other label is nonblank, unique, mapped to exactly one QID, and sorted alphabetically by label
- every needed field has field-specific structured evidence whose quoted excerpt exists in the cited packet tier/section; an already-effective field has null evidence
- every `somevalue` cause or manner has a complete four-tier `unknown_review` and a disposition for every indexed death-evidence candidate; concrete and already-effective death fields have null unknown reviews
- every proposed manner is derived or explicitly `somevalue` in this same pass
- every status is one of `possible_removal`, `settled`, `provisional`, `disputed`, or `unknown` and follows the stated priority
- `unknown` status has at least one effective unknown death field, while `settled` has none
- raw unresolved redirects and malformed source-anomaly records are rejected
- every proposal has a complete structured article-eligibility review; rejected English pages have every non-English sitelink dispositioned before selection or `possible_removal`
- every `possible_removal` has at least one allowed removal reason, proposes no new fallbacks, and is terminal for future cohort selection
- the protected non-fallback snapshot still matches the current CSV

If any condition fails, do not rewrite the public CSV.

FILE CHANGES

Update only these existing fallback columns in `age-27-people/age_27_people.csv`:

- `wikipedia_cause_of_death`
- `wikipedia_cause_of_death_qids`
- `wikipedia_manner_of_death`
- `wikipedia_manner_of_death_qids`
- `wikipedia_occupations`
- `wikipedia_occupation_qids`
- `wikipedia_death_review_status`

Serialize multiple values as aligned, alphabetically sorted lists separated by exactly `; ` in both label and QID columns. Do not modify a fallback field whose Wikidata base field is usable unless preserving an already populated dormant fallback.

Do not change age classification, population membership, Wikidata-derived fields, dates, base occupations, or musician taxonomy.

Keep a temporary/ignored per-person research log containing QID/name, article URL/language/resolved title/revision/bytes, structured field-specific evidence, unknown-review audits, source anomalies, proposed arrays, and status. Do not add evidence or instrumentation columns to the public CSV.

Merge every provisional, disputed, unknown, or possible-removal item into `age-27-people/wikipedia_stronger_model_review.csv` using this existing column order:

`wikidata_id,name,article_url,language,revision_id,article_bytes,status,proposed_cause,proposed_cause_qid,proposed_manner,proposed_manner_qid,proposed_occupation,proposed_occupation_qid,evidence_basis`

Populate the six `proposed_*` columns from the proposal arrays, not from the effective Wikidata fields. For an unknown proposed field, write literal `somevalue` in its label column and leave its paired QID column blank. If a proposal field is JSON `null` because its Wikidata or preserved fallback value was already effective, leave both corresponding `proposed_*` columns blank; do not copy the effective base value into this fallback-review file. Preserve any concrete proposals for the other fields normally. The flattened evidence basis must identify which death field remains unknown and summarize its completed individual audit, stating that the successfully retrieved complete article supplied no usable account (or explicitly reported it as unknown, unreported, pending, or unconfirmed).

Preserve prior-cohort rows, replace/update rows for selected QIDs, deduplicate by QID, and write the file once. Do not replace the cumulative queue with only the current batch.

Let the helper's final `verify --rebuild-browser` step rebuild `age-27-browser/data.js` twice and compare hashes. Do not rebuild it separately.

FINAL VALIDATION

Use the same dependency-capable interpreter selected at the start. The helper's `verify --run-tests` command runs the existing people, musician, and browser tests once after the public write. Rerun only a suite affected by a repair. It also verifies:

- exact public CSV and review-queue schemas
- aligned and sorted label/QID arrays using the exact `; ` delimiter
- every selected person is now effective for cause, manner, and identity
- blank dormant fallbacks with usable Wikidata base values are accepted
- every `somevalue` has no QID
- the browser payload rebuild is deterministic
- every musician QID remains in the people dataset and musician age/date fields match
- protected non-fallback cohort columns are unchanged
- review-queue QIDs are unique and previous rows remain
- `git diff --check` passes

INSTRUMENTATION

Record UTC timestamps for run start/end and these stages: selection, article retrieval, packet extraction, semantic inference, vocabulary resolution, staging/apply, and final validation.

Report:

- selected and completed counts
- end-to-end wall time and average per person
- per-stage elapsed times
- downloaded article bytes, cache hits, and average bytes per person
- estimated article-content tokens using bytes / 4
- settled, provisional, disputed, and unknown counts
- possible-removal count and reasons
- `somevalue` causes, manners, and identities
- count of individually audited unknown death fields and any source anomalies
- unique vocabulary labels, cache hits, actual external vocabulary lookups, HTTP retries, and failed requests
- tool-call count if directly available
- test results and stronger-model review items

If Codex input/output token telemetry is directly available, report it. Do not spend extra turns or tokens trying to recover unavailable telemetry.

FINAL RESPONSE

Lead with whether the entire fixed cohort was completed and validated. Give compact metrics, list only provisional/disputed/unknown cases individually, and link the changed people CSV, cumulative review CSV, and browser payload. Do not print a full cohort review table.
