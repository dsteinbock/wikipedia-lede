import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "enrichment_batch.py"
SPEC = importlib.util.spec_from_file_location("age27_enrichment_batch", MODULE_PATH)
batch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(batch)


BASE_COLUMNS = [
    "name",
    "wikipedia_url",
    "wikidata_id",
    "birth_date",
    "death_date",
    "cause_of_death",
    "manner_of_death",
    "occupations",
]
TEST_COLUMNS = BASE_COLUMNS + batch.FALLBACK_COLUMNS


def blank_row(qid, name, death):
    row = {column: "" for column in TEST_COLUMNS}
    row.update(
        {
            "name": name,
            "wikipedia_url": f"https://en.wikipedia.org/wiki/{name.replace(' ', '_')}",
            "wikidata_id": qid,
            "birth_date": "1997-01-01",
            "death_date": death,
        }
    )
    return row


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def evidence(source_tier, section, excerpt, reason):
    return {
        "source_tier": source_tier,
        "section": section,
        "excerpt": excerpt,
        "reason": reason,
    }


def primary_eligible(reason="The English article is a dedicated biography."):
    return {
        "decision": "eligible",
        "selected_article": "enwiki",
        "primary_review": {
            "page_kind": "person",
            "subject_is_human": "human",
            "life_status": "deceased",
            "age_compatibility": "compatible",
            "reason": reason,
        },
        "alternate_reviews": {},
        "removal_reasons": [],
    }


def semantic_page_review(**overrides):
    value = {
        "page_kind": "person",
        "subject_match": "match",
        "subject_is_human": "human",
        "life_status": "deceased",
        "age_compatibility": "compatible",
        "reason": "The packet is a dedicated biography of the deceased subject.",
    }
    value.update(overrides)
    return value


def install_artifact(run_dir, role, qid, value):
    source = run_dir / "worker-output" / role / f"{qid}.json"
    batch.atomic_write_json(source, value)
    return batch.record_semantic_artifact(
        cohort_value=run_dir, role=role, qid=qid, input_path=source
    )


def write_article_and_packet(cache, run_dir, qid, name, raw):
    article = {
        "wikidata_id": qid,
        "name": name,
        "article_url": f"https://en.wikipedia.org/wiki/{name.replace(' ', '_')}",
        "language": "en",
        "resolved_title": name,
        "revision_id": 100,
        "article_bytes": len(raw.encode()),
        "raw_wikitext": raw,
        "fetched_utc": batch.utc_now(),
    }
    batch.atomic_write_json(cache / "articles" / f"{qid}.json", article)
    packet = batch.build_packet(article)
    batch.atomic_write_json(run_dir / "packets" / f"{qid}.json", packet)
    return packet


class SelectionTests(unittest.TestCase):
    def test_latest_possible_date_handles_partial_alternatives(self):
        self.assertEqual(
            batch.parse_latest_possible_date("2023; 2023-06-09"),
            (2023, 12, 31),
        )
        self.assertEqual(
            batch.parse_latest_possible_date("2024-03; 2024-02-29"),
            (2024, 3, 31),
        )
        self.assertEqual(
            batch.parse_latest_possible_date("-0044-03-15"),
            (-44, 3, 15),
        )
        with self.assertRaisesRegex(batch.BatchError, "Invalid death-date"):
            batch.parse_latest_possible_date("2024/not-a-date")

    def test_selection_uses_effective_fields_and_deterministic_order(self):
        newest_b = blank_row("Q20", "Newest B", "2024-03")
        newest_a = blank_row("Q10", "Newest A", "2024")
        older = blank_row("Q30", "Older", "2023-12-31")
        complete = blank_row("Q40", "Complete", "2025")
        complete["wikipedia_cause_of_death"] = "somevalue"
        complete["wikipedia_manner_of_death"] = "somevalue"
        complete["wikipedia_occupations"] = "somevalue"
        novalue = blank_row("Q50", "No value", "2026")
        novalue["cause_of_death"] = "novalue"
        novalue["manner_of_death"] = "somevalue"
        novalue["occupations"] = "actor"

        selected, eligible_count = batch.select_eligible(
            [older, complete, newest_b, novalue, newest_a], 10
        )
        self.assertEqual(eligible_count, 4)
        self.assertEqual(
            [row["wikidata_id"] for row in selected],
            ["Q50", "Q10", "Q20", "Q30"],
        )

    def test_selection_excludes_removed_entries_and_manifest_allows_them_missing(self):
        removed = blank_row("Q20", "Removed", "2024")
        remaining = blank_row("Q10", "Remaining", "2023")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            ledger = root / "removed_entries.csv"
            write_csv(people, TEST_COLUMNS, [removed, remaining])
            ledger_row = {
                **removed,
                "removal_reason": "living",
                "removed_utc": batch.utc_now(),
                "source_run_dir": str(root / "run"),
            }
            write_csv(ledger, batch.removed_entry_columns(TEST_COLUMNS), [ledger_row])
            _, count = batch.select_eligible(
                [removed, remaining], 10, removed_qids={"Q20"}
            )
            self.assertEqual(count, 1)
            status = batch.eligibility_status(
                people,
                limit=10,
                target_manifest=None,
                removed_csv=ledger,
            )
            self.assertEqual(status["eligible_count"], 1)

    def test_status_is_read_only_and_reports_total(self):
        with tempfile.TemporaryDirectory() as directory:
            people = Path(directory) / "people.csv"
            rows = [
                blank_row("Q2", "Second", "2023"),
                blank_row("Q1", "First", "2024"),
            ]
            write_csv(people, TEST_COLUMNS, rows)
            before = people.read_bytes()
            status = batch.eligibility_status(people, limit=1)
            self.assertEqual(status["eligible_count"], 2)
            self.assertEqual(status["next"][0]["wikidata_id"], "Q1")
            self.assertEqual(people.read_bytes(), before)


class RetrievalAndPacketTests(unittest.TestCase):
    def test_api_client_compression_headers_and_maxlag_retry(self):
        class Response:
            def __init__(self, value):
                self.headers = {"Content-Encoding": "deflate"}
                self.payload = zlib.compress(json.dumps(value).encode())

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        responses = [
            Response({"error": {"code": "maxlag", "lag": 2, "info": "busy"}}),
            Response({"query": {"ok": True}}),
        ]
        requests = []
        sleeps = []
        original_open = batch.urllib.request.urlopen
        original_sleep = batch.time.sleep

        def fake_open(request, timeout):
            requests.append((request, timeout))
            return responses.pop(0)

        batch.urllib.request.urlopen = fake_open
        batch.time.sleep = sleeps.append
        for key in batch.HTTP_STATS:
            batch.HTTP_STATS[key] = 0
        try:
            payload = batch._api_json(
                "https://example.test/w/api.php", {"action": "query"}
            )
        finally:
            batch.urllib.request.urlopen = original_open
            batch.time.sleep = original_sleep
        headers = {key.casefold(): value for key, value in requests[0][0].header_items()}
        self.assertEqual(payload, {"query": {"ok": True}})
        self.assertIn("github.com/dsteinbock/wikipedia-lede/issues", headers["user-agent"])
        self.assertEqual(headers["accept-encoding"], "gzip, deflate")
        self.assertEqual(len(requests), 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(batch.HTTP_STATS["retries"], 1)

    def test_retry_after_accepts_seconds_and_http_date(self):
        self.assertEqual(batch._retry_after_seconds("17", default=1), 17)
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
        self.assertGreaterEqual(batch._retry_after_seconds(future, default=1), 59)

    def test_redirect_and_normalized_titles_are_resolved(self):
        payload = {
            "query": {
                "normalized": [{"from": "alpha_name", "to": "Alpha name"}],
                "redirects": [{"from": "Alpha name", "to": "Alpha Person"}],
                "pages": [
                    {
                        "title": "Alpha Person",
                        "revisions": [{"revid": 1}],
                    }
                ],
            }
        }
        pages = batch.parse_pages(payload, ["alpha_name"])
        self.assertEqual(pages["alpha_name"]["title"], "Alpha Person")

    def test_unresolved_redirect_is_not_usable_cached_article(self):
        selected = {
            "wikidata_id": "Q1",
            "wikipedia_url": "https://en.wikipedia.org/wiki/Alex_Example",
        }
        cached = {
            "wikidata_id": "Q1",
            "article_url": selected["wikipedia_url"],
            "language": "en",
            "resolved_title": "Alex Example",
            "revision_id": 1,
            "article_bytes": 30,
            "raw_wikitext": "#REDIRECT [[Alex Person]]",
            "fetched_utc": batch.utc_now(),
        }
        self.assertFalse(batch._cached_article_is_usable(cached, selected, 24))

    def test_packet_retains_citation_text_and_complete_body(self):
        marker = "END-OF-LONG-ARTICLE-EVIDENCE"
        raw = (
            "{{Infobox person\n| occupation = painter\n}}\n"
            "'''Alex Example''' was a painter. "
            "<ref>{{cite web|title=Coroner reports an overdose|"
            "quote=The death was accidental}}</ref>\n\n"
            "A second lead paragraph.\n"
            "==Death==\n"
            "<ref name=prior/>Alex Example lost control of his motorcycle, "
            "hit a light pole, and died at the scene.\n"
            + ("body evidence " * 1200)
            + marker
            + "<ref name=later>Later citation text</ref>"
        )
        packet = batch.build_packet(
            {
                "wikidata_id": "Q1",
                "name": "Alex Example",
                "article_url": "https://en.wikipedia.org/wiki/Alex_Example",
                "language": "en",
                "resolved_title": "Alex Example",
                "revision_id": 123,
                "article_bytes": len(raw.encode()),
                "raw_wikitext": raw,
            }
        )
        combined = " ".join(
            str(packet[key])
            for key in (
                "lead_sentence",
                "rest_of_lead_paragraph",
                "remaining_lead_section",
                "infobox",
                "rest_of_article",
            )
        )
        self.assertIn("Coroner reports an overdose", combined)
        self.assertIn("The death was accidental", combined)
        self.assertIn("painter", packet["infobox"])
        self.assertIn(marker, packet["rest_of_article"])
        self.assertEqual(packet["schema_version"], 3)
        self.assertEqual(packet["remaining_lead_section"], "A second lead paragraph.")
        self.assertEqual(packet["article_sections"][0]["heading"], "Death")
        self.assertIn("motorcycle", packet["article_sections"][0]["text"])
        self.assertNotIn("death_evidence_candidates", packet)

    def test_fetch_uses_bulk_redirect_options_and_qid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            write_csv(people, TEST_COLUMNS, [blank_row("Q1", "Alpha name", "2024")])
            batch.create_cohort(
                people_csv=people,
                cache_root=cache,
                batch_size=1,
                run_dir=run_dir,
            )
            calls = []
            original = batch._api_json

            def fake_api(endpoint, params, **kwargs):
                calls.append((endpoint, params))
                return {
                    "query": {
                        "normalized": [{"from": "Alpha name", "to": "Alpha Name"}],
                        "redirects": [{"from": "Alpha Name", "to": "Alpha Person"}],
                        "pages": [
                            {
                                "title": "Alpha Person",
                                "revisions": [
                                    {
                                        "revid": 42,
                                        "size": 12,
                                        "slots": {"main": {"content": "article text"}},
                                    }
                                ],
                            }
                        ],
                    }
                }

            batch._api_json = fake_api
            try:
                first = batch.fetch_articles(
                    cohort_value=run_dir, cache_root=cache
                )
                second = batch.fetch_articles(
                    cohort_value=run_dir, cache_root=cache
                )
            finally:
                batch._api_json = original
            self.assertEqual(first["downloaded"], 1)
            self.assertEqual(second["cache_hits"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]["redirects"], 1)
            self.assertEqual(calls[0][1]["converttitles"], 1)
            cached = json.loads((cache / "articles" / "Q1.json").read_text())
            self.assertEqual(cached["resolved_title"], "Alpha Person")
            self.assertEqual(cached["revision_id"], 42)

    def test_vocabulary_lookup_uses_wikidata_search_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposals = root / "proposals.jsonl"
            proposals.write_text("", encoding="utf-8")
            batch.atomic_write_json(
                root / "unresolved_vocabulary.json",
                [
                    {
                        "field": "cause",
                        "label": "gunshot wound",
                        "normalized_label": "gunshot wound",
                    }
                ],
            )
            calls = []
            original = batch._api_json

            def fake_api(endpoint, params, **kwargs):
                calls.append((endpoint, params, kwargs))
                return {
                    "search": [
                        {
                            "id": "Q2140674",
                            "label": "gunshot wound",
                            "description": "physical trauma caused by a firearm",
                        }
                    ]
                }

            batch._api_json = fake_api
            try:
                first = batch.lookup_vocabulary_candidates(
                    proposals_path=proposals, cache_root=root / "cache"
                )
                second = batch.lookup_vocabulary_candidates(
                    proposals_path=proposals, cache_root=root / "cache"
                )
            finally:
                batch._api_json = original
            self.assertEqual(first["external_lookups"], 1)
            self.assertEqual(second["cache_hits"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "https://www.wikidata.org/w/api.php")
            self.assertEqual(calls[0][1]["action"], "wbsearchentities")
            self.assertEqual(calls[0][1]["maxlag"], 5)
            candidates = json.loads(
                (root / "vocabulary_candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual(candidates["items"][0]["field"], "cause")
            self.assertEqual(candidates["items"][0]["candidates"][0]["id"], "Q2140674")
            assignment = json.loads(
                batch.build_vocabulary_input(
                    proposals_path=proposals,
                    field="cause",
                    label="gunshot wound",
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(assignment["role"], "vocabulary")
            self.assertEqual(assignment["label"], "gunshot wound")


class StagingAndApplyTests(unittest.TestCase):
    def test_known_vocabulary_staging_atomic_apply_and_cumulative_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            review = root / "review.csv"
            run_dir = root / "run"

            first = blank_row("Q1", "First Person", "2024")
            second = blank_row("Q2", "Second Person", "2023")
            second["cause_of_death"] = "cancer"
            second["occupations"] = "actor"
            established = blank_row("Q3", "Established", "2022")
            established["wikipedia_cause_of_death"] = "gunshot wound"
            established["wikipedia_cause_of_death_qids"] = "Q2140674"
            established["manner_of_death"] = "homicide"
            established["occupations"] = "actor"
            write_csv(people, TEST_COLUMNS, [first, second, established])

            batch.create_cohort(
                people_csv=people,
                cache_root=cache,
                batch_size=2,
                run_dir=run_dir,
            )
            proposals_path = batch.init_proposals(cohort_value=run_dir)
            proposals = batch.load_proposals(proposals_path)
            by_qid = {item["wikidata_id"]: item for item in proposals}
            by_qid["Q1"].update(
                {
                    "article_eligibility": primary_eligible(),
                    "cause": [{"label": "gunshot wound", "qid": ""}],
                    "manner": [{"label": "homicide", "qid": "Q149086"}],
                    "occupation": [{"label": "murder victim", "qid": "Q73153647"}],
                    "status": "provisional",
                    "evidence_basis": {
                        "cause": evidence(
                            "rest_of_article",
                            "Death",
                            "First Person was shot and killed.",
                            "The death passage directly describes a shooting.",
                        ),
                        "manner": evidence(
                            "rest_of_article",
                            "Death",
                            "First Person was shot and killed.",
                            "The circumstances support homicide.",
                        ),
                        "occupation": evidence(
                            "lead_sentence",
                            "Lead sentence",
                            "First Person was a murder victim.",
                            "The lead identifies the subject as a murder victim.",
                        ),
                    },
                }
            )
            by_qid["Q2"].update(
                {
                    "article_eligibility": primary_eligible(),
                    "manner": [{"label": "somevalue", "qid": ""}],
                    "status": "unknown",
                    "evidence_basis": {
                        "cause": None,
                        "manner": evidence(
                            "none",
                            "",
                            "",
                            "The complete article supplies no usable manner of death.",
                        ),
                        "occupation": None,
                    },
                    "unknown_review": {
                        "cause": None,
                        "manner": {
                            "reviewed_source_tiers": batch.DEATH_REVIEW_TIERS,
                            "candidate_dispositions": {},
                            "conclusion": "The complete article supplies no usable manner of death.",
                        },
                    },
                }
            )
            batch.write_proposals(proposals_path, proposals)

            unresolved = batch.resolve_known_vocabulary(
                proposals_path=proposals_path,
                people_csv=people,
                vocabulary_path=cache / "vocabulary.json",
            )
            self.assertEqual(unresolved, [])
            proposals = {
                item["wikidata_id"]: item
                for item in batch.load_proposals(proposals_path)
            }
            self.assertEqual(proposals["Q1"]["cause"][0]["qid"], "Q2140674")

            write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "First Person",
                "'''First Person''' was a murder victim.\n\n==Death==\n"
                "First Person was shot and killed.",
            )
            write_article_and_packet(
                cache,
                run_dir,
                "Q2",
                "Second Person",
                "'''Second Person''' was an actor.",
            )

            write_csv(
                review,
                batch.REVIEW_COLUMNS,
                [
                    {
                        **{column: "" for column in batch.REVIEW_COLUMNS},
                        "wikidata_id": "Q99",
                        "name": "Prior Review",
                        "status": "disputed",
                    }
                ],
            )
            validated = batch.validate_proposals(
                cohort_value=run_dir,
                proposals_path=proposals_path,
                people_csv=people,
                cache_root=cache,
            )
            self.assertEqual(validated["validated"], 2)
            self.assertEqual(validated["audited_unknown_fields"], 1)

            result = batch.apply_proposals(
                cohort_value=run_dir,
                proposals_path=proposals_path,
                people_csv=people,
                review_csv=review,
                cache_root=cache,
            )
            self.assertEqual(result["review_queue_rows"], 3)
            _, rows = batch.read_csv(people)
            rows = {row["wikidata_id"]: row for row in rows}
            self.assertEqual(rows["Q1"]["wikipedia_cause_of_death"], "gunshot wound")
            self.assertEqual(
                rows["Q1"]["wikipedia_cause_of_death_qids"], "Q2140674"
            )
            self.assertEqual(rows["Q2"]["wikipedia_cause_of_death"], "")
            self.assertEqual(
                rows["Q2"]["wikipedia_manner_of_death"], "somevalue"
            )
            self.assertEqual(rows["Q3"]["wikipedia_cause_of_death"], "gunshot wound")
            _, review_rows = batch.read_csv(review)
            review_by_qid = {row["wikidata_id"]: row for row in review_rows}
            self.assertEqual(
                set(review_by_qid), {"Q1", "Q2", "Q99"}
            )
            self.assertEqual(review_by_qid["Q2"]["proposed_cause"], "")
            self.assertEqual(review_by_qid["Q2"]["proposed_cause_qid"], "")
            self.assertEqual(review_by_qid["Q2"]["proposed_manner"], "somevalue")
            self.assertEqual(review_by_qid["Q2"]["proposed_manner_qid"], "")
            self.assertEqual(review_by_qid["Q2"]["proposed_occupation"], "")
            self.assertIn("no usable manner", review_by_qid["Q2"]["evidence_basis"])

    def test_validation_rejects_missing_mapping_before_public_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            write_csv(people, TEST_COLUMNS, [blank_row("Q1", "Person", "2024")])
            batch.create_cohort(
                people_csv=people,
                cache_root=cache,
                batch_size=1,
                run_dir=run_dir,
            )
            proposals_path = batch.init_proposals(cohort_value=run_dir)
            proposals = batch.load_proposals(proposals_path)
            proposals[0].update(
                {
                    "article_eligibility": primary_eligible(),
                    "cause": [{"label": "specific condition", "qid": ""}],
                    "manner": [{"label": "somevalue", "qid": ""}],
                    "occupation": [{"label": "somevalue", "qid": ""}],
                    "status": "unknown",
                    "evidence_basis": {
                        "cause": evidence(
                            "lead_sentence",
                            "Lead sentence",
                            "Person died of a specific condition.",
                            "The article supplies a condition but mapping is unresolved.",
                        ),
                        "manner": evidence(
                            "none", "", "", "No manner can be established."
                        ),
                        "occupation": evidence(
                            "none", "", "", "No identity can be established."
                        ),
                    },
                }
            )
            batch.write_proposals(proposals_path, proposals)
            write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "Person",
                "'''Person''' died of a specific condition.",
            )
            before = people.read_bytes()
            with self.assertRaisesRegex(batch.BatchError, "missing/invalid QID"):
                batch.validate_proposals(
                    cohort_value=run_dir,
                    proposals_path=proposals_path,
                    people_csv=people,
                    cache_root=cache,
                )
            self.assertEqual(people.read_bytes(), before)

    def test_semantic_classification_rejects_unknown_evidence_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            row = blank_row("Q1", "Person", "2024")
            row["cause_of_death"] = "cancer"
            row["occupations"] = "actor"
            write_csv(people, TEST_COLUMNS, [row])
            batch.create_cohort(
                people_csv=people,
                cache_root=cache,
                batch_size=1,
                run_dir=run_dir,
            )
            write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "Person",
                "'''Person''' was an actor.\n\n==Death==\n"
                "Person died after an illness, but no manner was reported.",
            )
            batch.atomic_write_json(
                run_dir / "semantic" / "death-evidence" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "evidence": [
                        {
                            "evidence_id": "E1",
                            "source_tier": "rest_of_article",
                            "section": "Death",
                            "text": "Person died after an illness.",
                        }
                    ],
                    "no_usable_account": {"cause": False, "manner": True},
                    "reason": "The article reports no manner of death.",
                },
            )
            batch.atomic_write_json(
                run_dir / "semantic" / "death-classification" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "cause": None,
                    "manner": [{"label": "somevalue", "qid": ""}],
                    "status": "unknown",
                    "evidence_ids": {"cause": [], "manner": ["E99"]},
                    "reason": "No usable manner account exists.",
                },
            )
            evidence_artifact = batch._validate_death_evidence(run_dir, "Q1")
            with self.assertRaisesRegex(batch.BatchError, "unknown or duplicate"):
                batch._validate_death_classification(
                    run_dir,
                    "Q1",
                    {"cause": False, "manner": True, "occupation": False},
                    evidence_artifact,
                )

    def test_semantic_classification_settles_any_usable_account(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            evidence_artifact = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "evidence": [
                    {
                        "evidence_id": "E1",
                        "source_tier": "rest_of_article",
                        "section": "Death",
                        "text": "It was reported that the person died by suicide.",
                    }
                ],
                "no_usable_account": {"cause": True, "manner": False},
                "reason": "The article reports a manner but no physical mechanism.",
            }
            batch.atomic_write_json(
                run_dir / "semantic" / "death-classification" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "cause": [{"label": "somevalue", "qid": ""}],
                    "manner": [{"label": "suicide", "qid": ""}],
                    "status": "settled",
                    "evidence_ids": {"cause": [], "manner": ["E1"]},
                    "reason": "Any reported account is settled under project policy.",
                },
            )
            parsed = batch._validate_death_classification(
                run_dir,
                "Q1",
                {"cause": True, "manner": True, "occupation": False},
                evidence_artifact,
            )
            self.assertEqual(parsed["status"], "settled")

    def test_semantic_classification_rejects_provisional(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            evidence_artifact = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "evidence": [
                    {
                        "evidence_id": "E1",
                        "source_tier": "rest_of_article",
                        "section": "Death",
                        "text": "A possible heart attack was reported.",
                    }
                ],
                "no_usable_account": {"cause": False, "manner": False},
                "reason": "The article offers a possible account.",
            }
            batch.atomic_write_json(
                run_dir / "semantic" / "death-classification" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "cause": [{"label": "myocardial infarction", "qid": ""}],
                    "manner": [{"label": "natural causes", "qid": ""}],
                    "status": "provisional",
                    "evidence_ids": {"cause": ["E1"], "manner": ["E1"]},
                    "reason": "The account was described as possible.",
                },
            )
            with self.assertRaisesRegex(
                batch.BatchError, "invalid death classification status"
            ):
                batch._validate_death_classification(
                    run_dir,
                    "Q1",
                    {"cause": True, "manner": True, "occupation": False},
                    evidence_artifact,
                )

    def test_semantic_classification_may_infer_manner_from_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            evidence_artifact = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "evidence": [
                    {
                        "evidence_id": "E1",
                        "source_tier": "rest_of_article",
                        "section": "Illness and death",
                        "text": "She had cancer, her health worsened, and she died.",
                    }
                ],
                "no_usable_account": {"cause": False, "manner": True},
                "reason": "Cancer is described but manner is not separately labeled.",
            }
            batch.atomic_write_json(
                run_dir / "semantic" / "death-classification" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "cause": [{"label": "cancer", "qid": ""}],
                    "manner": [{"label": "natural causes", "qid": ""}],
                    "status": "settled",
                    "evidence_ids": {"cause": ["E1"], "manner": ["E1"]},
                    "reason": "Natural causes is inferred directly from cancer.",
                },
            )
            parsed = batch._validate_death_classification(
                run_dir,
                "Q1",
                {"cause": True, "manner": True, "occupation": False},
                evidence_artifact,
            )
            self.assertEqual(parsed["manner"][0]["label"], "natural causes")

    def test_possible_removal_is_queued_terminal_and_preserves_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            review = root / "review.csv"
            cache = root / "cache"
            run_dir = root / "run"
            row = blank_row("Q1", "Living Person", "2024")
            row["wikipedia_cause_of_death"] = "somevalue"
            write_csv(people, TEST_COLUMNS, [row])
            batch.create_cohort(
                people_csv=people,
                cache_root=cache,
                batch_size=1,
                run_dir=run_dir,
            )
            write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "Living Person",
                "'''Living Person''' is a living actor.",
            )
            proposals_path = batch.init_proposals(cohort_value=run_dir)
            proposals = batch.load_proposals(proposals_path)
            proposals[0].update(
                {
                    "cause": None,
                    "manner": None,
                    "occupation": None,
                    "status": "possible_removal",
                    "evidence_basis": {"cause": None, "manner": None, "occupation": None},
                    "article_eligibility": {
                        "decision": "possible_removal",
                        "selected_article": "enwiki",
                        "primary_review": {
                            "page_kind": "person",
                            "subject_is_human": "human",
                            "life_status": "living",
                            "age_compatibility": "unknown",
                            "reason": "The dedicated biography describes the subject as living.",
                        },
                        "alternate_reviews": {},
                        "removal_reasons": ["living"],
                    },
                }
            )
            batch.write_proposals(proposals_path, proposals)
            validated = batch.validate_proposals(
                cohort_value=run_dir,
                proposals_path=proposals_path,
                people_csv=people,
                cache_root=cache,
            )
            self.assertEqual(validated["status_counts"]["possible_removal"], 1)
            result = batch.apply_proposals(
                cohort_value=run_dir,
                proposals_path=proposals_path,
                people_csv=people,
                review_csv=review,
                cache_root=cache,
            )
            self.assertEqual(result["review_queue_rows"], 1)
            _, rows = batch.read_csv(people)
            self.assertEqual(rows[0]["wikipedia_cause_of_death"], "somevalue")
            self.assertEqual(rows[0]["wikipedia_death_review_status"], "possible_removal")
            selected, eligible_count = batch.select_eligible(rows, 10)
            self.assertEqual((selected, eligible_count), ([], 0))

    def test_migrate_removes_possible_removal_and_appends_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live_people = root / "live.csv"
            staged_people = root / "staged.csv"
            live_review = root / "live-review.csv"
            staged_review = root / "staged-review.csv"
            removed = root / "removed_entries.csv"
            cache = root / "cache"
            run_dir = root / "run"
            rows = [blank_row("Q1", "Living Person", "2024"), blank_row("Q2", "Other", "2023")]
            write_csv(live_people, TEST_COLUMNS, rows)
            write_csv(staged_people, TEST_COLUMNS, rows)
            write_csv(live_review, batch.REVIEW_COLUMNS, [{
                **{column: "" for column in batch.REVIEW_COLUMNS},
                "wikidata_id": "Q1",
                "status": "possible_removal",
            }])
            write_csv(staged_review, batch.REVIEW_COLUMNS, [])
            batch.create_cohort(
                people_csv=staged_people,
                cache_root=cache,
                batch_size=2,
                run_dir=run_dir,
            )
            proposals = [
                {
                    "wikidata_id": "Q1",
                    "status": "possible_removal",
                    "article_eligibility": {"removal_reasons": ["living"]},
                },
                {"wikidata_id": "Q2", "status": "settled"},
            ]
            proposals_path = run_dir / "proposals.jsonl"
            batch.write_proposals(proposals_path, proposals)
            original_validator = batch.validate_proposals
            batch.validate_proposals = lambda **_kwargs: {}
            try:
                result = batch.migrate_approved_cohort(
                    cohort_value=run_dir,
                    proposals_path=proposals_path,
                    staged_people_csv=staged_people,
                    staged_review_csv=staged_review,
                    live_people_csv=live_people,
                    live_review_csv=live_review,
                    removed_csv=removed,
                    cache_root=cache,
                )
            finally:
                batch.validate_proposals = original_validator
            self.assertEqual(result["removed_rows"], 1)
            _, live_rows = batch.read_csv(live_people)
            self.assertEqual([row["wikidata_id"] for row in live_rows], ["Q2"])
            _, ledger_rows = batch.read_csv(removed)
            self.assertEqual(ledger_rows[0]["wikidata_id"], "Q1")
            self.assertEqual(ledger_rows[0]["removal_reason"], "living")
            _, review_rows = batch.read_csv(live_review)
            self.assertEqual(review_rows, [])
            batch.validate_proposals = lambda **_kwargs: {}
            try:
                repeated = batch.migrate_approved_cohort(
                    cohort_value=run_dir,
                    proposals_path=proposals_path,
                    staged_people_csv=staged_people,
                    staged_review_csv=staged_review,
                    live_people_csv=live_people,
                    live_review_csv=live_review,
                    removed_csv=removed,
                    cache_root=cache,
                )
            finally:
                batch.validate_proposals = original_validator
            self.assertEqual(repeated["removed_rows"], 0)
            self.assertEqual(repeated["already_ledgered_rows"], 1)
            self.assertEqual(len(batch.read_csv(removed)[1]), 1)

    def test_non_english_person_article_redeems_english_list_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            row = blank_row("Q1", "Inbar Example", "2023")
            row["cause_of_death"] = "murder"
            row["manner_of_death"] = "homicide"
            write_csv(people, TEST_COLUMNS, [row])
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=1, run_dir=run_dir
            )
            write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "Inbar Example",
                "'''Hostage list''' contains many named people.",
            )
            alternate = {
                "wikidata_id": "Q1",
                "name": "Inbar Example",
                "article_url": "https://he.wikipedia.org/wiki/Inbar_Example",
                "language": "he",
                "requested_title": "Inbar Example",
                "resolved_title": "Inbar Example",
                "revision_id": 200,
                "article_bytes": 100,
                "raw_wikitext": "'''Inbar Example''' was an artist.",
                "fetched_utc": batch.utc_now(),
            }
            article_path = cache / "article-alternates" / "Q1" / "hewiki.json"
            packet_path = run_dir / "alternate-packets" / "Q1" / "hewiki.json"
            batch.atomic_write_json(article_path, alternate)
            batch.atomic_write_json(packet_path, batch.build_packet(alternate))
            batch.atomic_write_json(
                run_dir / "alternate_article_index.json",
                {
                    "Q1": [
                        {
                            "candidate_id": "hewiki",
                            "language": "he",
                            "article_url": alternate["article_url"],
                            "resolved_title": alternate["resolved_title"],
                            "revision_id": 200,
                            "article_bytes": 100,
                            "article_cache": str(article_path),
                            "packet": str(packet_path.relative_to(run_dir)),
                        }
                    ]
                },
            )
            proposals_path = batch.init_proposals(cohort_value=run_dir)
            proposals = batch.load_proposals(proposals_path)
            proposals[0].update(
                {
                    "occupation": [{"label": "artist", "qid": "Q483501"}],
                    "status": "settled",
                    "evidence_basis": {
                        "cause": None,
                        "manner": None,
                        "occupation": evidence(
                            "lead_sentence",
                            "Lead sentence",
                            "Inbar Example was an artist.",
                            "The dedicated Hebrew biography identifies her as an artist.",
                        ),
                    },
                    "article_eligibility": {
                        "decision": "eligible",
                        "selected_article": "hewiki",
                        "primary_review": {
                            "page_kind": "list",
                            "subject_is_human": "human",
                            "life_status": "unclear",
                            "age_compatibility": "unknown",
                            "reason": "The English target is a hostage list, not a biography.",
                        },
                        "alternate_reviews": {
                            "hewiki": {
                                "page_kind": "person",
                                "subject_is_human": "human",
                                "life_status": "deceased",
                                "age_compatibility": "compatible",
                                "reason": "The Hebrew article is dedicated to the individual.",
                            }
                        },
                        "removal_reasons": [],
                    },
                }
            )
            batch.write_proposals(proposals_path, proposals)
            result = batch.validate_proposals(
                cohort_value=run_dir,
                proposals_path=proposals_path,
                people_csv=people,
                cache_root=cache,
            )
            self.assertEqual(result["validated"], 1)


class ParallelSemanticPipelineTests(unittest.TestCase):
    def test_artifacts_are_owned_immutable_and_stream_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            row = blank_row("Q1", "Person", "2024")
            write_csv(people, TEST_COLUMNS, [row])
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=1, run_dir=run_dir
            )
            write_article_and_packet(
                cache, run_dir, "Q1", "Person", "'''Person''' was an actor."
            )
            eligibility = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "reviews": {"enwiki": semantic_page_review()},
            }
            install_artifact(run_dir, "eligibility", "Q1", eligibility)
            with self.assertRaisesRegex(batch.BatchError, "already exists"):
                install_artifact(run_dir, "eligibility", "Q1", eligibility)
            outside = run_dir / "outside.json"
            batch.atomic_write_json(
                outside,
                {"schema_version": 1, "wikidata_id": "Q999", "reviews": {}},
            )
            with self.assertRaisesRegex(batch.BatchError, "outside the cohort"):
                batch.record_semantic_artifact(
                    cohort_value=run_dir,
                    role="eligibility",
                    qid="Q999",
                    input_path=outside,
                )

            batch.aggregate_eligibility(cohort_value=run_dir)
            first_status = batch.stage_status(cohort_value=run_dir)["people"][0]["roles"]
            self.assertEqual(first_status["death-evidence"], "ready")
            self.assertEqual(first_status["death-classification"], "blocked")
            self.assertEqual(first_status["identity"], "ready")
            batch.mark_semantic_task(
                cohort_value=run_dir,
                role="death-evidence",
                qid="Q1",
                status="running",
            )
            self.assertEqual(
                batch.stage_status(cohort_value=run_dir)["people"][0]["roles"][
                    "death-evidence"
                ],
                "running",
            )

            install_artifact(
                run_dir,
                "death-evidence",
                "Q1",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "evidence": [
                        {
                            "evidence_id": "E1",
                            "source_tier": "lead_sentence",
                            "section": "Lead",
                            "text": "Person died in an accident.",
                        }
                    ],
                    "no_usable_account": {"cause": False, "manner": False},
                    "reason": "The lead supplies the death account.",
                },
            )
            second_status = batch.stage_status(cohort_value=run_dir)["people"][0]["roles"]
            self.assertEqual(second_status["death-classification"], "ready")
            identity_input = json.loads(
                batch.build_role_input(
                    cohort_value=run_dir, role="identity", qid="Q1"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(identity_input["lead"]),
                {
                    "lead_sentence",
                    "rest_of_lead_paragraph",
                    "remaining_lead_section",
                    "infobox",
                },
            )
            self.assertNotIn("rest_of_article", identity_input["lead"])
            classification_input = json.loads(
                batch.build_role_input(
                    cohort_value=run_dir,
                    role="death-classification",
                    qid="Q1",
                    vocabulary_path=root / "approved.json",
                ).read_text(encoding="utf-8")
            )
            self.assertIn("homicide", classification_input["canonical_labels"]["cause"])
            self.assertEqual(
                set(classification_input["status_definitions"]),
                {"settled", "unknown"},
            )
            self.assertEqual(
                classification_input["evidence_bundle"]["evidence"][0]["evidence_id"],
                "E1",
            )
            batch.atomic_write_json(
                run_dir / "semantic" / "identity" / "Q999.json",
                {"schema_version": 1, "wikidata_id": "Q999"},
            )
            with self.assertRaisesRegex(batch.BatchError, "Out-of-cohort identity"):
                batch.assemble_semantic_proposals(cohort_value=run_dir)

    def test_largest_qualifying_alternate_is_selected_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            write_csv(people, TEST_COLUMNS, [blank_row("Q1", "Person", "2024")])
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=1, run_dir=run_dir
            )
            write_article_and_packet(
                cache, run_dir, "Q1", "Person", "'''A list''' contains people."
            )
            index = {"Q1": []}
            for candidate_id, size in (("dewiki", 100), ("frwiki", 200)):
                packet = run_dir / "alternate-packets" / "Q1" / f"{candidate_id}.json"
                batch.atomic_write_json(packet, {"schema_version": 3})
                index["Q1"].append(
                    {
                        "candidate_id": candidate_id,
                        "article_bytes": size,
                        "packet": str(packet.relative_to(run_dir)),
                    }
                )
            batch.atomic_write_json(run_dir / "alternate_article_index.json", index)
            install_artifact(
                run_dir,
                "eligibility",
                "Q1",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "reviews": {
                        "enwiki": semantic_page_review(
                            page_kind="list",
                            subject_match="mismatch",
                            reason="The English target is a list.",
                        )
                    },
                },
            )
            install_artifact(
                run_dir,
                "alternate-eligibility",
                "Q1",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "reviews": {
                        "dewiki": semantic_page_review(),
                        "frwiki": semantic_page_review(),
                    },
                },
            )
            result = batch.aggregate_eligibility(cohort_value=run_dir)
            self.assertEqual(result["people"]["Q1"]["selected_article"], "frwiki")

    def test_alternate_fetch_is_a_serial_noop_when_all_english_pages_qualify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            write_csv(people, TEST_COLUMNS, [blank_row("Q1", "Person", "2024")])
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=1, run_dir=run_dir
            )
            install_artifact(
                run_dir,
                "eligibility",
                "Q1",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "reviews": {"enwiki": semantic_page_review()},
                },
            )
            result = batch.fetch_alternate_articles(
                cohort_value=run_dir, proposals_path=None, cache_root=cache
            )
            self.assertEqual(
                result,
                {"requested_people": 0, "alternate_articles": 0, "article_bytes": 0},
            )

    def test_failed_qid_does_not_block_ready_work_but_blocks_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            rows = [blank_row("Q1", "First", "2024"), blank_row("Q2", "Second", "2023")]
            for row in rows:
                row["occupations"] = "actor"
            write_csv(people, TEST_COLUMNS, rows)
            before = people.read_bytes()
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=2, run_dir=run_dir
            )
            for qid, name in (("Q1", "First"), ("Q2", "Second")):
                write_article_and_packet(
                    cache,
                    run_dir,
                    qid,
                    name,
                    f"'''{name}''' was an actor.\n\n==Death==\n{name} died in a crash.",
                )
                install_artifact(
                    run_dir,
                    "eligibility",
                    qid,
                    {
                        "schema_version": 1,
                        "wikidata_id": qid,
                        "reviews": {"enwiki": semantic_page_review()},
                    },
                )
            batch.aggregate_eligibility(cohort_value=run_dir)
            install_artifact(
                run_dir,
                "death-evidence",
                "Q1",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "evidence": [],
                    "no_usable_account": False,
                    "reason": "Malformed worker result for the failure-path test.",
                },
            )
            install_artifact(
                run_dir,
                "death-evidence",
                "Q2",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q2",
                    "evidence": [
                        {
                            "evidence_id": "E1",
                            "source_tier": "rest_of_article",
                            "section": "Death",
                            "text": "Second died in a crash.",
                        }
                    ],
                    "no_usable_account": {"cause": False, "manner": False},
                    "reason": "The death account is usable.",
                },
            )
            states = {
                item["wikidata_id"]: item["roles"]
                for item in batch.stage_status(cohort_value=run_dir)["people"]
            }
            self.assertEqual(states["Q1"]["death-evidence"], "failed")
            self.assertEqual(states["Q1"]["death-classification"], "blocked")
            self.assertEqual(states["Q2"]["death-evidence"], "complete")
            self.assertEqual(states["Q2"]["death-classification"], "ready")
            with self.assertRaisesRegex(batch.BatchError, "no_usable_account"):
                batch.assemble_semantic_proposals(cohort_value=run_dir)
            self.assertEqual(people.read_bytes(), before)

    def test_regressions_assemble_in_cohort_order_and_resolve_trusted_qids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            rows = []
            for qid, name, manner, occupation in (
                ("Q1", "Gladys Aranza Ramos Gurrola", "homicide", "activist"),
                ("Q2", "Koyan Chamitha", "", "naval officer"),
                ("Q3", "Hayden Kennedy", "suicide", "mountaineer"),
            ):
                row = blank_row(qid, name, "2024")
                row["manner_of_death"] = manner
                row["occupations"] = occupation
                rows.append(row)
            write_csv(people, TEST_COLUMNS, rows)
            public_before = people.read_bytes()
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=3, run_dir=run_dir
            )
            passages = {
                "Q1": "She was murdered while searching for her husband. Cause: homicide.",
                "Q2": "Initial reports indicated a possible heart attack; a formal inquiry was opened to determine the exact cause.",
                "Q3": "Kennedy died by suicide.",
            }
            names = {row["wikidata_id"]: row["name"] for row in rows}
            for qid in ("Q3", "Q1", "Q2"):
                write_article_and_packet(
                    cache,
                    run_dir,
                    qid,
                    names[qid],
                    f"'''{names[qid]}''' was notable.\n\n==Death==\n{passages[qid]}",
                )
                install_artifact(
                    run_dir,
                    "eligibility",
                    qid,
                    {
                        "schema_version": 1,
                        "wikidata_id": qid,
                        "reviews": {"enwiki": semantic_page_review()},
                    },
                )
            batch.aggregate_eligibility(cohort_value=run_dir)

            classifications = {
                "Q1": ("homicide", None, "settled"),
                "Q2": ("myocardial infarction", "natural causes", "settled"),
                "Q3": ("suicide", None, "settled"),
            }
            for qid in ("Q2", "Q3", "Q1"):
                install_artifact(
                    run_dir,
                    "death-evidence",
                    qid,
                    {
                        "schema_version": 1,
                        "wikidata_id": qid,
                        "evidence": [
                            {
                                "evidence_id": "E1",
                                "source_tier": "rest_of_article",
                                "section": "Death",
                                "text": passages[qid],
                            }
                        ],
                        "no_usable_account": {"cause": False, "manner": False},
                        "reason": "The complete death passage was extracted.",
                    },
                )
                cause, manner, status = classifications[qid]
                install_artifact(
                    run_dir,
                    "death-classification",
                    qid,
                    {
                        "schema_version": 1,
                        "wikidata_id": qid,
                        "cause": [{"label": cause, "qid": ""}],
                        "manner": (
                            [{"label": manner, "qid": ""}]
                            if manner is not None
                            else None
                        ),
                        "status": status,
                        "evidence_ids": {
                            "cause": ["E1"],
                            "manner": ["E1"] if manner is not None else [],
                        },
                        "reason": "The evidence directly supports this classification.",
                    },
                )

            proposals_path = run_dir / "proposals.jsonl"
            batch.assemble_semantic_proposals(
                cohort_value=run_dir, output=proposals_path
            )
            proposals = batch.load_proposals(proposals_path)
            self.assertEqual([item["wikidata_id"] for item in proposals], ["Q1", "Q2", "Q3"])
            self.assertEqual([item["status"] for item in proposals], ["settled", "settled", "settled"])
            unresolved = batch.resolve_known_vocabulary(
                proposals_path=proposals_path,
                people_csv=people,
                vocabulary_path=root / "approved-vocabulary.json",
            )
            self.assertEqual(unresolved, [])
            resolved = {item["wikidata_id"]: item for item in batch.load_proposals(proposals_path)}
            self.assertEqual(resolved["Q1"]["cause"], [{"label": "homicide", "qid": "Q149086"}])
            self.assertEqual(resolved["Q2"]["cause"], [{"label": "myocardial infarction", "qid": "Q12152"}])
            self.assertEqual(resolved["Q3"]["cause"], [{"label": "suicide", "qid": "Q10737"}])
            validated = batch.validate_proposals(
                cohort_value=run_dir,
                proposals_path=proposals_path,
                people_csv=people,
                cache_root=cache,
            )
            self.assertEqual(validated["validated"], 3)
            self.assertEqual(people.read_bytes(), public_before)

    def test_vocabulary_is_field_specific_and_not_fuzzy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted.json"
            batch.atomic_write_json(
                trusted,
                {
                    "schema_version": 1,
                    "mappings": {
                        "cause": {"shared label": {"label": "Shared Label", "qid": "Q1"}},
                        "manner": {"shared label": {"label": "Shared Label", "qid": "Q2"}},
                        "occupation": {},
                    },
                },
            )
            proposals = root / "proposals.jsonl"
            batch.write_proposals(
                proposals,
                [
                    {
                        "wikidata_id": "Q10",
                        "cause": [{"label": "  SHARED   LABEL ", "qid": ""}],
                        "manner": [{"label": "shared label", "qid": ""}],
                        "occupation": [{"label": "shared labels", "qid": ""}],
                    }
                ],
            )
            unresolved = batch.resolve_known_vocabulary(
                proposals_path=proposals,
                people_csv=root / "unused.csv",
                vocabulary_path=root / "approved.json",
                trusted_vocabulary_path=trusted,
            )
            resolved = batch.load_proposals(proposals)[0]
            self.assertEqual(resolved["cause"][0]["qid"], "Q1")
            self.assertEqual(resolved["manner"][0]["qid"], "Q2")
            self.assertEqual(
                unresolved,
                [
                    {
                        "field": "occupation",
                        "label": "shared labels",
                        "normalized_label": "shared labels",
                    }
                ],
            )

    def test_novel_vocabulary_artifact_applies_and_persists_reviewed_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            write_csv(people, TEST_COLUMNS, [blank_row("Q1", "Person", "2024")])
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=1, run_dir=run_dir
            )
            proposals = run_dir / "proposals.jsonl"
            batch.write_proposals(
                proposals,
                [{"wikidata_id": "Q1", "cause": [{"label": "novel label", "qid": ""}]}],
            )
            unresolved = [
                {
                    "field": "cause",
                    "label": "novel label",
                    "normalized_label": "novel label",
                }
            ]
            batch.atomic_write_json(run_dir / "unresolved_vocabulary.json", unresolved)
            batch.atomic_write_json(
                run_dir / "vocabulary_candidates.json",
                {
                    "schema_version": 1,
                    "items": [
                        {
                            **unresolved[0],
                            "candidates": [
                                {
                                    "id": "Q123",
                                    "label": "Canonical novel concept",
                                    "description": "the exact requested concept",
                                }
                            ],
                        }
                    ],
                },
            )
            worker_output = root / "vocabulary-result.json"
            batch.atomic_write_json(
                worker_output,
                {
                    "schema_version": 1,
                    "field": "cause",
                    "label": "novel label",
                    "decision": "approved",
                    "selected_qid": "Q123",
                    "reason": "The candidate expresses the exact concept.",
                },
            )
            batch.record_vocabulary_artifact(
                cohort_value=run_dir, input_path=worker_output
            )
            approved = root / "approved.json"
            result = batch.apply_vocabulary_artifacts(
                cohort_value=run_dir,
                proposals_path=proposals,
                vocabulary_path=approved,
            )
            self.assertEqual(result, {"resolved": 1})
            self.assertEqual(
                batch.load_proposals(proposals)[0]["cause"],
                [{"label": "Canonical novel concept", "qid": "Q123"}],
            )
            stored = json.loads(approved.read_text(encoding="utf-8"))
            self.assertEqual(stored["mappings"]["cause"]["novel label"]["qid"], "Q123")
            followup = root / "followup.jsonl"
            batch.write_proposals(
                followup,
                [{"wikidata_id": "Q2", "cause": [{"label": "Novel Label", "qid": ""}]}],
            )
            self.assertEqual(
                batch.resolve_known_vocabulary(
                    proposals_path=followup,
                    people_csv=people,
                    vocabulary_path=approved,
                ),
                [],
            )
            self.assertEqual(
                batch.load_proposals(followup)[0]["cause"],
                [{"label": "Canonical novel concept", "qid": "Q123"}],
            )


class RedoPreparationTests(unittest.TestCase):
    def test_prepare_redo_backs_up_clears_and_freezes_exact_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            review = root / "review.csv"
            backup = root / "backup"
            targets = []
            for number, death in enumerate(("2024", "2023", "2022"), 1):
                row = blank_row(f"Q{number}", f"Target {number}", death)
                row["wikipedia_cause_of_death"] = "somevalue"
                row["wikipedia_manner_of_death"] = "somevalue"
                row["wikipedia_occupations"] = "somevalue"
                row["wikipedia_death_review_status"] = "unknown"
                targets.append(row)
            other = blank_row("Q9", "Other", "2021")
            write_csv(people, TEST_COLUMNS, targets + [other])
            write_csv(
                review,
                batch.REVIEW_COLUMNS,
                [
                    {**{column: "" for column in batch.REVIEW_COLUMNS}, "wikidata_id": "Q1"},
                    {**{column: "" for column in batch.REVIEW_COLUMNS}, "wikidata_id": "Q99"},
                ],
            )
            original_nonfallback = {
                row["wikidata_id"]: {
                    column: row[column]
                    for column in TEST_COLUMNS
                    if column not in batch.FALLBACK_COLUMNS
                }
                for row in targets + [other]
            }
            result = batch.prepare_redo(
                people_csv=people,
                review_csv=review,
                backup_dir=backup,
                expected_count=3,
                rebuild_browser=False,
            )
            self.assertEqual(result["expected_cohort_sizes"], [3])
            _, backed_up = batch.read_csv(backup / "people_rows.csv")
            self.assertEqual(len(backed_up), 3)
            manifest = batch.load_target_manifest(backup / "target-manifest.json")
            self.assertEqual(
                [item["wikidata_id"] for item in manifest["people"]],
                ["Q1", "Q2", "Q3"],
            )
            _, cleared = batch.read_csv(people)
            for row in cleared:
                self.assertEqual(
                    {
                        column: row[column]
                        for column in TEST_COLUMNS
                        if column not in batch.FALLBACK_COLUMNS
                    },
                    original_nonfallback[row["wikidata_id"]],
                )
                if row["wikidata_id"] in {"Q1", "Q2", "Q3"}:
                    self.assertTrue(all(not row[column] for column in batch.FALLBACK_COLUMNS))
            _, remaining_review = batch.read_csv(review)
            self.assertEqual([row["wikidata_id"] for row in remaining_review], ["Q99"])
            status = batch.eligibility_status(
                people, 100, backup / "target-manifest.json"
            )
            self.assertEqual(status["target_remaining"], 3)


class RefillableSchedulerTests(unittest.TestCase):
    def test_all_eligible_selection_freezes_approval_tranches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            rows = [blank_row(f"Q{index}", f"Person {index}", "2024") for index in range(1, 206)]
            write_csv(people, TEST_COLUMNS, rows)
            run_dir = root / "run"
            batch.create_cohort(
                people_csv=people,
                cache_root=root / "cache",
                batch_size=None,
                run_dir=run_dir,
            )
            cohort = json.loads((run_dir / "cohort.json").read_text(encoding="utf-8"))
            self.assertEqual(cohort["selection_mode"], "all_eligible")
            self.assertEqual(cohort["selected_count"], 205)
            self.assertEqual(
                [cohort["selected"][index]["approval_tranche"] for index in (0, 99, 100, 199, 200)],
                [1, 1, 2, 2, 3],
            )

    def test_claims_enforce_six_slots_and_byte_capped_singletons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            rows = [blank_row(f"Q{index}", f"Person {index}", "2024") for index in range(1, 8)]
            write_csv(people, TEST_COLUMNS, rows)
            run_dir = root / "run"
            cache = root / "cache"
            batch.create_cohort(people_csv=people, cache_root=cache, batch_size=None, run_dir=run_dir)
            for row in rows:
                write_article_and_packet(
                    cache,
                    run_dir,
                    row["wikidata_id"],
                    row["name"],
                    f"'''{row['name']}''' was notable.\n" + ("x" * 300_000),
                )
            direct = root / "direct.json"
            batch.atomic_write_json(
                direct,
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "reviews": {"enwiki": semantic_page_review()},
                },
            )
            with self.assertRaisesRegex(batch.BatchError, "complete-assignment"):
                batch.record_semantic_artifact(
                    cohort_value=run_dir,
                    role="eligibility",
                    qid="Q1",
                    input_path=direct,
                )
            claims = [
                batch.claim_assignment(cohort_value=run_dir, slot=slot)
                for slot in (1, 2, 3, 4, 5, 6)
            ]
            self.assertTrue(all(item["role"] == "eligibility" for item in claims))
            self.assertTrue(all(len(item["items"]) == 1 for item in claims))
            self.assertTrue(all(item["items"][0]["oversize_singleton"] for item in claims))
            with self.assertRaisesRegex(batch.BatchError, "Slot must be 1..6"):
                batch.claim_assignment(cohort_value=run_dir, slot=7)
            with self.assertRaisesRegex(batch.BatchError, "active lease"):
                batch.claim_assignment(cohort_value=run_dir, slot=1)

    def test_failed_work_gets_two_recoveries_then_enters_exception_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            row = blank_row("Q1", "Person", "2024")
            write_csv(people, TEST_COLUMNS, [row])
            batch.create_cohort(people_csv=people, cache_root=cache, batch_size=None, run_dir=run_dir)
            write_article_and_packet(cache, run_dir, "Q1", "Person", "'''Person''' was notable.")
            for attempt in range(1, 4):
                claim = batch.claim_assignment(cohort_value=run_dir, slot=1)
                result = batch.complete_assignment(
                    cohort_value=run_dir,
                    assignment_id=claim["assignment_id"],
                    input_path=None,
                    failed_reason=f"diagnosed failure {attempt}",
                )
            self.assertEqual(result["failed"][0]["attempts"], "3")
            status = batch.scheduler_status(cohort_value=run_dir)
            self.assertEqual(len(status["exceptions"]), 1)
            self.assertTrue(status["tranches"][0]["reviewable"])
            self.assertEqual(status["ready_counts"]["eligibility"], 0)
            self.assertEqual(status["processing_remaining"], 0)
            self.assertTrue(status["processing_complete"])
            self.assertEqual(status["completion_scope"], "frozen_cohort_only")

    def test_completion_preserves_valid_partial_results_and_rejects_extras(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            rows = [blank_row("Q1", "First", "2024"), blank_row("Q2", "Second", "2023")]
            write_csv(people, TEST_COLUMNS, rows)
            batch.create_cohort(people_csv=people, cache_root=cache, batch_size=None, run_dir=run_dir)
            for row in rows:
                write_article_and_packet(cache, run_dir, row["wikidata_id"], row["name"], f"'''{row['name']}''' was notable.")
            claim = batch.claim_assignment(cohort_value=run_dir, slot=1)
            output = root / "result.json"
            batch.atomic_write_json(
                output,
                [
                    {
                        "schema_version": 1,
                        "wikidata_id": "Q1",
                        "reviews": {"enwiki": semantic_page_review()},
                    },
                    {
                        "schema_version": 1,
                        "wikidata_id": "Q999",
                        "reviews": {"enwiki": semantic_page_review()},
                    },
                ],
            )
            result = batch.complete_assignment(
                cohort_value=run_dir,
                assignment_id=claim["assignment_id"],
                input_path=output,
            )
            self.assertEqual(result["installed"], ["Q1"])
            self.assertEqual(result["rejected_extra_keys"], ["Q999"])
            self.assertTrue(batch.semantic_artifact_path(run_dir, "eligibility", "Q1").exists())
            self.assertFalse(batch.semantic_artifact_path(run_dir, "eligibility", "Q999").exists())

    def test_exception_review_links_names_sorts_fields_and_keeps_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            rows = [
                blank_row("Q1", "Zulu", "2024"),
                blank_row("Q2", "Alpha", "2024"),
            ]
            write_csv(people, TEST_COLUMNS, rows)
            run_dir = root / "run"
            batch.create_cohort(
                people_csv=people, cache_root=root / "cache", batch_size=None, run_dir=run_dir
            )
            batch.atomic_write_json(
                run_dir / "scheduler" / "exceptions.json",
                {
                    "vocabulary:occupation:zulu": {
                        "task_key": "vocabulary:occupation:zulu",
                        "role": "vocabulary",
                        "key": "occupation:zulu",
                        "reason": "No direct occupation item.",
                        "affected_qids": ["Q1"],
                    },
                    "vocabulary:cause:alpha": {
                        "task_key": "vocabulary:cause:alpha",
                        "role": "vocabulary",
                        "key": "cause:alpha",
                        "reason": "No direct cause item.",
                        "affected_qids": ["Q2"],
                    },
                },
            )
            candidates = root / "candidate-proposals.json"
            batch.atomic_write_json(
                candidates,
                {
                    "schema_version": 1,
                    "rows": [
                        {
                            "exception_key": "vocabulary:occupation:zulu",
                            "qid": "Q1",
                            "name": "Zulu",
                            "wikipedia_url": rows[0]["wikipedia_url"],
                            "field": "occupation",
                            "label": "zulu",
                            "reason": "No direct occupation item.",
                            "proposed_mappings": [
                                {"label": "artist", "qid": "Q1", "rationale": "Review candidate."}
                            ],
                        },
                        {
                            "exception_key": "vocabulary:cause:alpha",
                            "qid": "Q2",
                            "name": "Alpha",
                            "wikipedia_url": rows[1]["wikipedia_url"],
                            "field": "cause",
                            "label": "alpha",
                            "reason": "No direct cause item.",
                            "proposed_mappings": [
                                {"label": "injury", "qid": "Q2", "rationale": "Review candidate."}
                            ],
                        },
                    ],
                },
            )
            output = root / "exception-review.json"
            result = batch.prepare_exception_review(
                cohort_value=run_dir,
                tranche=1,
                candidate_proposals=candidates,
                staged_people_csv=people,
                output=output,
            )
            self.assertEqual(result["rows"], 2)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([item["field"] for item in payload["rows"]], ["cause", "occupation"])
            markdown = output.with_suffix(".md").read_text(encoding="utf-8")
            self.assertIn("[Alpha](https://en.wikipedia.org/wiki/Alpha)", markdown)
            self.assertIn("[Q1](https://www.wikidata.org/wiki/Q1)", markdown)
            self.assertNotIn("| Wikipedia |", markdown)

    def test_tranche_approval_hash_detects_post_review_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            review_csv = root / "review.csv"
            cache = root / "cache"
            run_dir = root / "run"
            musicians = root / "musicians.csv"
            archive = root / "archive.html"
            row = blank_row("Q1", "Person", "2024")
            write_csv(people, TEST_COLUMNS, [row])
            write_csv(musicians, ["wikidata_id"], [])
            archive.write_text("<table></table>", encoding="utf-8")
            batch.create_cohort(people_csv=people, cache_root=cache, batch_size=None, run_dir=run_dir)
            write_article_and_packet(cache, run_dir, "Q1", "Person", "'''Person''' is alive.")
            claim = batch.claim_assignment(cohort_value=run_dir, slot=1)
            worker_output = root / "eligibility-result.json"
            batch.atomic_write_json(
                worker_output,
                [{
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "reviews": {
                        "enwiki": semantic_page_review(
                            life_status="living",
                            reason="The biography says the subject is living.",
                        )
                    },
                }],
            )
            batch.complete_assignment(
                cohort_value=run_dir,
                assignment_id=claim["assignment_id"],
                input_path=worker_output,
            )
            original_musicians = batch.MUSICIANS_CSV
            original_archive = batch.CLUB_ARCHIVE_HTML
            try:
                batch.MUSICIANS_CSV = musicians
                batch.CLUB_ARCHIVE_HTML = archive
                batch.atomic_write_json(
                    run_dir / "alternate_article_index.json", {"Q1": []}
                )
                batch.aggregate_eligibility(cohort_value=run_dir, ready_only=True)
                proposals = run_dir / "tranches" / "001" / "proposals.jsonl"
                batch.assemble_semantic_proposals(
                    cohort_value=run_dir, output=proposals, tranche=1
                )
                batch.apply_proposals(
                    cohort_value=run_dir,
                    proposals_path=proposals,
                    people_csv=people,
                    review_csv=review_csv,
                    cache_root=cache,
                    tranche=1,
                )
                verified = batch.verify_batch(
                    cohort_value=run_dir,
                    people_csv=people,
                    musicians_csv=musicians,
                    review_csv=review_csv,
                    rebuild_browser=False,
                    run_tests=False,
                    expected_qids=["Q1"],
                )
                self.assertEqual(verified["completed"], 1)
                prepared = batch.prepare_tranche_review(
                    cohort_value=run_dir,
                    tranche=1,
                    proposals_path=proposals,
                    staged_people_csv=people,
                    staged_review_csv=review_csv,
                )
                manifest = Path(prepared["review_manifest"])
                with self.assertRaisesRegex(batch.BatchError, "does not match"):
                    batch.record_tranche_approval(
                        review_manifest=manifest, reviewed_hash="0" * 64
                    )
                approval = batch.record_tranche_approval(
                    review_manifest=manifest, reviewed_hash=prepared["review_hash"]
                )
                _, staged_rows = batch.read_csv(people)
                staged_rows[0]["name"] = "Changed after review"
                write_csv(people, TEST_COLUMNS, staged_rows)
                cohort = batch.cohort_paths(run_dir)[1]
                with self.assertRaisesRegex(batch.BatchError, "changed after approval"):
                    batch._validated_approval(cohort=cohort, approval_path=approval)
            finally:
                batch.MUSICIANS_CSV = original_musicians
                batch.CLUB_ARCHIVE_HTML = original_archive

    def test_review_correction_does_not_invalidate_accepted_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            review_csv = root / "review.csv"
            proposals = root / "proposals.jsonl"
            run_dir = root / "run"
            rows = [
                blank_row("Q1", "Accepted", "2024"),
                blank_row("Q2", "Needs correction", "2023"),
            ]
            write_csv(people, TEST_COLUMNS, rows)
            write_csv(review_csv, batch.REVIEW_COLUMNS, [])
            batch.write_proposals(
                proposals,
                [{"wikidata_id": "Q1"}, {"wikidata_id": "Q2"}],
            )
            batch.create_cohort(
                people_csv=people,
                cache_root=root / "cache",
                batch_size=None,
                run_dir=run_dir,
            )
            cohort = batch.cohort_paths(run_dir)[1]
            qids = ["Q1", "Q2"]
            payload = batch._review_payload(
                qids=qids,
                proposals_path=proposals,
                staged_people_csv=people,
                staged_review_csv=review_csv,
            )
            review_hash = hashlib.sha256(
                batch.canonical_json(payload).encode()
            ).hexdigest()
            manifest = run_dir / "scheduler" / "reviews" / "tranche-001.json"
            batch.atomic_write_json(
                manifest,
                {
                    "schema_version": 1,
                    "cohort_hash": cohort["cohort_hash"],
                    "tranche": 1,
                    "qids": qids,
                    "proposals": str(proposals),
                    "staged_people_csv": str(people),
                    "staged_review_csv": str(review_csv),
                    "review_hash": review_hash,
                },
            )
            correction = batch.queue_review_correction(
                cohort_value=run_dir,
                review_manifest=manifest,
                qid="Q2",
                reason="User requested a different occupation",
            )
            self.assertEqual(correction["role"], "correction")
            updated_review = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(set(updated_review["item_hashes"]), set(qids))
            approval_path = batch.record_tranche_approval(
                review_manifest=manifest,
                reviewed_hash=review_hash,
            )
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            self.assertEqual(approval["approved_qids"], ["Q1"])
            self.assertEqual(approval["correction_qids"], ["Q2"])

            _, changed_rows = batch.read_csv(people)
            changed_rows[1]["name"] = "Corrected after review"
            write_csv(people, TEST_COLUMNS, changed_rows)
            batch._validated_approval(cohort=cohort, approval_path=approval_path)

            changed_rows[0]["name"] = "Accepted row changed"
            write_csv(people, TEST_COLUMNS, changed_rows)
            with self.assertRaisesRegex(batch.BatchError, "Reviewed item changed"):
                batch._validated_approval(cohort=cohort, approval_path=approval_path)


class ArticleEligibilityTests(unittest.TestCase):
    def test_no_dedicated_article_stays_for_musician_or_archived_club_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            rows = [
                blank_row("Q1", "Musician Example", "2024"),
                blank_row("Q2", "Archived Example", "2024"),
            ]
            rows[0]["occupations"] = "bassist"
            write_csv(people, TEST_COLUMNS, rows)
            batch.create_cohort(
                people_csv=people, cache_root=cache, batch_size=2, run_dir=run_dir
            )
            for qid, name in (("Q1", "Musician Example"), ("Q2", "Archived Example")):
                write_article_and_packet(
                    cache,
                    run_dir,
                    qid,
                    name,
                    f"'''{name}''' redirects to a group or list entry.",
                )
                install_artifact(
                    run_dir,
                    "eligibility",
                    qid,
                    {
                        "schema_version": 1,
                        "wikidata_id": qid,
                        "reviews": {
                            "enwiki": semantic_page_review(
                                page_kind="group",
                                age_compatibility="unknown",
                                reason="The final page is not a dedicated biography.",
                            )
                        },
                    },
                )
            musicians_csv = root / "musicians.csv"
            write_csv(musicians_csv, ["wikidata_id"], [{"wikidata_id": "Q1"}])
            archive_html = root / "purported-27-club-members.html"
            archive_html.write_text(
                "<table><tr><th>Name</th></tr>"
                '<tr><td><a href="https://en.wikipedia.org/wiki/Archived_Example">'
                "Archived Example</a></td></tr></table>",
                encoding="utf-8",
            )
            original_musicians = batch.MUSICIANS_CSV
            original_archive = batch.CLUB_ARCHIVE_HTML
            batch.MUSICIANS_CSV = musicians_csv
            batch.CLUB_ARCHIVE_HTML = archive_html
            try:
                selected = batch.aggregate_eligibility(cohort_value=run_dir)
                self.assertEqual(
                    selected["people"]["Q1"]["stay_overrides"],
                    ["approved_musician_occupation"],
                )
                self.assertEqual(
                    selected["people"]["Q2"]["stay_overrides"],
                    ["archived_27_club_article"],
                )
                self.assertEqual(
                    selected["people"]["Q1"]["decision"], "eligible"
                )
                self.assertEqual(
                    selected["people"]["Q2"]["decision"], "eligible"
                )
            finally:
                batch.MUSICIANS_CSV = original_musicians
                batch.CLUB_ARCHIVE_HTML = original_archive

    def test_possible_removal_reason_variants_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            packet = batch.build_packet(
                {
                    "wikidata_id": "Q1",
                    "name": "Example",
                    "article_url": "https://en.wikipedia.org/wiki/Example",
                    "language": "en",
                    "resolved_title": "Example",
                    "revision_id": 1,
                    "article_bytes": 20,
                    "raw_wikitext": "'''Example''' was notable.",
                }
            )
            batch.atomic_write_json(run_dir / "packets" / "Q1.json", packet)

            base = {
                "decision": "possible_removal",
                "selected_article": "enwiki",
                "primary_review": {
                    "page_kind": "person",
                    "subject_is_human": "human",
                    "life_status": "deceased",
                    "age_compatibility": "compatible",
                    "reason": "Structured eligibility evidence was reviewed.",
                },
                "alternate_reviews": {},
                "removal_reasons": [],
            }
            scenarios = [
                ("nonhuman", "subject_is_human", "nonhuman"),
                ("age_outside_26_28", "age_compatibility", "outside_26_28"),
                ("age_outside_26_28", "age_compatibility", "conflicting"),
                ("subject_identity_mismatch", "page_kind", "person"),
            ]
            for reason, key, value in scenarios:
                with self.subTest(reason=reason, value=value):
                    review = json.loads(json.dumps(base))
                    review["primary_review"][key] = value
                    review["removal_reasons"] = [reason]
                    validated, _, _ = batch._validate_article_eligibility(
                        qid="Q1", value=review, run_dir=run_dir
                    )
                    self.assertEqual(validated["decision"], "possible_removal")

            batch.atomic_write_json(run_dir / "alternate_article_index.json", {"Q1": []})
            no_page = json.loads(json.dumps(base))
            no_page["selected_article"] = ""
            no_page["primary_review"]["page_kind"] = "case"
            no_page["removal_reasons"] = ["no_dedicated_person_article"]
            validated, _, _ = batch._validate_article_eligibility(
                qid="Q1", value=no_page, run_dir=run_dir
            )
            self.assertEqual(
                validated["removal_reasons"], ["no_dedicated_person_article"]
            )


class AmbiguousMemberReviewTests(unittest.TestCase):
    def test_mixed_repair_manifest_requires_exact_lane_classes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed-repair.json"
            targets = []
            for number, target_class in [
                *[(number, "current_none") for number in range(1, 149)],
                *[(number, "grandfathered_absent") for number in range(149, 220)],
            ]:
                targets.append(
                    {
                        "wikidata_id": f"Q{number}",
                        "target_class": target_class,
                        "current_report_action": "none" if target_class == "current_none" else "absent",
                        "report_row_sha256": "a" * 64 if target_class == "current_none" else None,
                        "source_row_sha256": "b" * 64,
                        "state_entry_sha256": "c" * 64,
                    }
                )
            batch.atomic_write_json(
                path,
                {
                    "schema_version": 1,
                    "lane": "ambiguous_members_mixed_repair",
                    "review_policy_version": batch.AMBIGUOUS_REVIEW_POLICY_VERSION,
                    "created_utc": "2026-09-13T00:00:00+00:00",
                    "people_csv": "people.csv",
                    "people_sha256": "d" * 64,
                    "report_csv": "report.csv",
                    "report_sha256": "e" * 64,
                    "state_path": "state.json",
                    "state_sha256": "f" * 64,
                    "target_count": 219,
                    "target_counts": {"current_none": 148, "grandfathered_absent": 71},
                    "targets": targets,
                    "preserved_report_rows": {},
                    "preserved_state_members": {},
                },
            )
            loaded = batch._load_ambiguous_repair_manifest(path)
            self.assertEqual(len(batch._mixed_repair_targets(loaded)), 219)

    def test_none_repair_manifest_selects_only_frozen_none_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            report = root / "review.csv"
            state_path = root / "state.json"
            manifest_path = root / "repair.json"
            run_dir = root / "run"
            none_person = blank_row("Q1", "None Person", "2024")
            approved_person = blank_row("Q2", "Approved Person", "2024")
            for row in (none_person, approved_person):
                row["age_status"] = "possible"
            write_csv(
                people,
                TEST_COLUMNS + ["age_status"],
                [none_person, approved_person],
            )

            report_rows = []
            for person, action in ((none_person, "none"), (approved_person, "discard")):
                row = {column: "" for column in batch.AMBIGUOUS_REVIEW_COLUMNS}
                row.update(
                    {
                        "wikidata_id": person["wikidata_id"],
                        "name": person["name"],
                        "source_row_sha256": batch._ambiguous_source_row_sha256(person),
                        "recommended_action": action,
                        "source_run_dir": str(root / "old-run"),
                    }
                )
                report_rows.append(row)
            write_csv(report, batch.AMBIGUOUS_REVIEW_COLUMNS, report_rows)
            batch.atomic_write_json(
                state_path,
                {
                    "schema_version": 1,
                    "members": {
                        person["wikidata_id"]: {
                            "source_row_sha256": batch._ambiguous_source_row_sha256(person),
                            "source_run_dir": str(root / "old-run"),
                        }
                        for person in (none_person, approved_person)
                    },
                },
            )
            prepared = batch.prepare_ambiguous_repair_manifest(
                people_csv=people,
                review_csv=report,
                state_path=state_path,
                output=manifest_path,
                action="none",
                expected_count=1,
            )
            self.assertEqual(prepared["target_count"], 1)
            self.assertEqual(prepared["preserved_report_rows"], 1)
            batch.create_ambiguous_cohort(
                people_csv=people,
                cache_root=root / "cache",
                state_path=state_path,
                batch_size=None,
                run_dir=run_dir,
                target_manifest=manifest_path,
                review_csv=report,
            )
            cohort = json.loads((run_dir / "cohort.json").read_text())
            self.assertEqual(cohort["selection_mode"], "target_manifest")
            self.assertEqual(
                [item["wikidata_id"] for item in cohort["selected"]], ["Q1"]
            )

    def test_field_date_parser_excludes_citations_and_template_cross_field_values(self):
        self.assertEqual(
            batch._date_values_from_field(
                '1845<ref>{{cite book|date=1926|access-date=17 May 2021}}</ref>',
                "birth_date",
            ),
            ["1845"],
        )
        raw = "{{death year and age|1919|1892}}"
        self.assertEqual(batch._date_values_from_field(raw, "death_date"), ["1919"])
        self.assertEqual(batch._template_date_values(raw, "birth_date"), ["1892"])
        exact = "{{death-date and age|9 July 1934|15 May 1907}}"
        self.assertEqual(
            batch._date_values_from_field(exact, "death_date"), ["1934-07-09"]
        )

    def test_field_date_parser_preserves_alternatives_and_uses_gregorian_conversion(self):
        self.assertEqual(
            batch._date_values_from_field("1379/80", "birth_date"),
            ["1379", "1380"],
        )
        self.assertEqual(
            batch._date_values_from_field(
                "1926 A.D. (1983 BS Mangshir 10)", "birth_date"
            ),
            ["1926"],
        )
        self.assertEqual(
            batch._date_values_from_field(
                "Chaitra, 1852 Bikram Samwat (1796 A.D.)", "birth_date"
            ),
            ["1796"],
        )
        self.assertEqual(
            batch._date_values_from_field("{{circa}} 1050s", "birth_date"),
            [],
        )

    def test_scanner_keeps_out_of_range_infobox_age_as_one_range(self):
        raw = (
            "{{Infobox person\n| birth_date = 1379/80\n"
            "| death_date = 19 October 1408 (aged 29–30)<ref>2020-08-15</ref>\n}}\n"
            "'''Person''' was a ruler.\n\n==Death==\nHe died at the age of 28."
        )
        article = {
            "wikidata_id": "Q1",
            "name": "Person",
            "article_url": "https://en.wikipedia.org/wiki/Person",
            "language": "en",
            "resolved_title": "Person",
            "revision_id": 1,
            "article_bytes": len(raw.encode()),
            "raw_wikitext": raw,
        }
        person = {
            "wikidata_id": "Q1",
            "name": "Person",
            "member_classes": ["possible"],
            "age_status": "possible",
            "birth_date": "1380",
            "death_date": "1408",
            "source_row_sha256": "hash",
        }
        record = batch._scan_ambiguous_article(
            person, article, batch.build_packet(article)
        )
        self.assertEqual(record["ambiguous_infobox_ages"], [29, 30])
        self.assertEqual(
            [item["value"] for item in record["candidates"] if item["kind"] == "age_range"],
            ["29-30"],
        )
        self.assertFalse(
            any(
                item["kind"] == "age" and item["source"] == "infobox"
                for item in record["candidates"]
            )
        )
        self.assertNotIn(
            "2020-08-15",
            [item["value"] for item in record["candidates"]],
        )

    def test_sentence_context_restores_death_context_and_omits_bibliography(self):
        raw = (
            "{{Infobox person|birth_date=1982|death_date=11 November 2009}}\n"
            "'''Person''' was an activist.\n\n==Death==\n"
            "He was executed after being sentenced to death. He was 28 years old.\n\n"
            "==References==\n* Journal 27 (2019), pp. 26–28."
        )
        article = {
            "wikidata_id": "Q1",
            "name": "Person",
            "article_url": "https://en.wikipedia.org/wiki/Person",
            "language": "en",
            "resolved_title": "Person",
            "revision_id": 1,
            "article_bytes": len(raw.encode()),
            "raw_wikitext": raw,
        }
        person = {
            "wikidata_id": "Q1",
            "name": "Person",
            "member_classes": ["possible"],
            "age_status": "possible",
            "birth_date": "1982",
            "death_date": "2009",
            "source_row_sha256": "hash",
        }
        record = batch._scan_ambiguous_article(
            person, article, batch.build_packet(article)
        )
        body_ages = [
            item for item in record["candidates"] if item["source"] == "article_body"
        ]
        self.assertEqual([item["value"] for item in body_ages], [28])
        self.assertIn("executed", body_ages[0]["excerpt"])

    def test_scanner_reads_actual_lead_lifespan_after_short_description(self):
        raw = (
            "{{Short description|French biologist (1860–1888)}}\n"
            "{{Infobox person|birth_date=1860|death_date=1888}}\n"
            "'''Person''' (8 January 1860 – 31 March 1888) was a biologist."
        )
        article = {
            "wikidata_id": "Q1",
            "name": "Person",
            "article_url": "https://en.wikipedia.org/wiki/Person",
            "language": "en",
            "resolved_title": "Person",
            "revision_id": 1,
            "article_bytes": len(raw.encode()),
            "raw_wikitext": raw,
        }
        person = {
            "wikidata_id": "Q1",
            "name": "Person",
            "member_classes": ["possible"],
            "age_status": "possible",
            "birth_date": "1860-08-01",
            "death_date": "1888",
            "source_row_sha256": "hash",
        }
        record = batch._scan_ambiguous_article(
            person, article, batch.build_packet(article)
        )
        lead_dates = {
            (item.get("deterministic_claim_type"), item["value"])
            for item in record["candidates"]
            if item["kind"] == "date" and item["source"] == "lead"
        }
        self.assertIn(("birth_date", "1860-01-08"), lead_dates)
        self.assertIn(("death_date", "1888-03-31"), lead_dates)

    def test_body_age_ranges_are_not_singular_but_explicit_age_29_is(self):
        text = "One source says aged 27–28. Foster died in March 1826 aged 29."
        singular, ranges = batch._explicit_body_age_hits(text)
        self.assertEqual([age for _, _, age in singular], [29])
        self.assertEqual([(low, high) for _, _, low, high in ranges], [(27, 28)])

    def test_selection_is_independent_of_existing_enrichment_terminal_state(self):
        possible = blank_row("Q1", "Possible", "2024")
        possible["age_status"] = "possible"
        possible["cause_of_death"] = "cancer"
        possible["manner_of_death"] = "natural causes"
        possible["occupations"] = "actor"
        possible["wikipedia_death_review_status"] = "settled"
        multi_date = blank_row("Q2", "Multi", "2023")
        multi_date["age_status"] = "confirmed"
        multi_date["birth_date"] = "1995-01-01; 1995-01-02"
        ordinary = blank_row("Q3", "Ordinary", "2022")
        ordinary["age_status"] = "confirmed"

        selected, eligible, pending = batch.select_ambiguous_members(
            [ordinary, multi_date, possible], batch_size=None
        )
        self.assertEqual(eligible, 2)
        self.assertEqual(pending, 2)
        self.assertEqual([row["wikidata_id"] for row in selected], ["Q1", "Q2"])
        self.assertEqual(batch.ambiguous_member_classes(possible), ["possible"])
        self.assertEqual(
            batch.ambiguous_member_classes(multi_date), ["multiple_birth_dates"]
        )

        state = {
            "review_policy_version": batch.AMBIGUOUS_REVIEW_POLICY_VERSION,
            "members": {
                "Q1": {
                    "source_row_sha256": batch._ambiguous_source_row_sha256(possible)
                }
            }
        }
        selected, _, pending = batch.select_ambiguous_members(
            [ordinary, multi_date, possible], batch_size=None, state=state
        )
        self.assertEqual(pending, 1)
        self.assertEqual([row["wikidata_id"] for row in selected], ["Q2"])
        selected, _, pending = batch.select_ambiguous_members(
            [ordinary, multi_date, possible],
            batch_size=None,
            state=state,
            rescan_all=True,
        )
        self.assertEqual(pending, 2)
        self.assertEqual(len(selected), 2)

    def test_scanner_uses_visible_body_and_citation_titles_but_not_metadata(self):
        raw = (
            "{{Infobox person\n"
            "| birth_date = {{Birth date|1995|7|14}}\n"
            "| death_date = {{Death date and age|2022|7|26|1995|7|14}}\n"
            "}}\n"
            "'''Alex Person''' was an actor born July 14, 1995.\n\n"
            "==Death==\n"
            "The crash happened on 26 July 2022. "
            "<ref>{{cite web|url=https://example.test/28/report|"
            "title=Actor was 27 when he died|publisher=Channel 28}}</ref>"
        )
        article = {
            "wikidata_id": "Q1",
            "name": "Alex Person",
            "article_url": "https://en.wikipedia.org/wiki/Alex_Person",
            "language": "en",
            "resolved_title": "Alex Person",
            "revision_id": 123,
            "article_bytes": len(raw.encode()),
            "raw_wikitext": raw,
        }
        packet = batch.build_packet(article)
        person = {
            "wikidata_id": "Q1",
            "name": "Alex Person",
            "member_classes": ["possible"],
            "age_status": "possible",
            "birth_date": "1995",
            "death_date": "2022-07-26",
            "source_row_sha256": "hash",
        }
        record = batch._scan_ambiguous_article(person, article, packet)
        self.assertEqual(record["canonical_infobox_age"], 27)
        citation_ages = [
            item["value"]
            for item in record["candidates"]
            if item["source"] == "citation_title" and item["kind"] == "age"
        ]
        self.assertEqual(citation_ages, [27])
        self.assertNotIn(28, citation_ages)
        self.assertTrue(
            any(
                item["kind"] == "date"
                and item["source"] == "infobox"
                and item["value"] == "1995-07-14"
                for item in record["candidates"]
            )
        )
        self.assertNotIn("Channel 28", batch._visible_article_body(raw))
        self.assertNotIn("example.test/28", batch._visible_article_body(raw))
        self.assertEqual(
            batch._citation_titles(
                '<ref>[https://example.test/27/report Person was 28]</ref>'
            ),
            ["Person was 28"],
        )

    def test_lower_precision_wikipedia_date_is_not_a_candidate(self):
        raw = (
            "{{Infobox person\n"
            "| birth_date = 1995\n"
            "| death_date = 26 July 2022\n"
            "}}\n'''Alex Person''' was an actor."
        )
        article = {
            "wikidata_id": "Q1",
            "name": "Alex Person",
            "article_url": "https://en.wikipedia.org/wiki/Alex_Person",
            "language": "en",
            "resolved_title": "Alex Person",
            "revision_id": 1,
            "article_bytes": len(raw.encode()),
            "raw_wikitext": raw,
        }
        person = {
            "wikidata_id": "Q1",
            "name": "Alex Person",
            "member_classes": ["multiple_birth_dates"],
            "age_status": "confirmed",
            "birth_date": "1995-07-14; 1995-07-15",
            "death_date": "2022",
            "source_row_sha256": "hash",
        }
        record = batch._scan_ambiguous_article(
            person, article, batch.build_packet(article)
        )
        infobox_dates = {
            (item["deterministic_claim_type"], item["value"])
            for item in record["candidates"]
            if item["kind"] == "date" and item["source"] == "infobox"
        }
        self.assertNotIn(("birth_date", "1995"), infobox_dates)
        self.assertIn(("death_date", "2022-07-26"), infobox_dates)

    def test_given_age_template_is_canonical_and_keeps_date_alternatives(self):
        raw = (
            "{{Infobox person\n"
            "| birth_date = 1997 or 1998\n"
            "| death_date = {{death date and given age|2025|11|01|27|df=y}}\n"
            "}}\n'''Omid Example''' (1997 or 1998 – 1 November 2025) was an activist."
        )
        article = {
            "wikidata_id": "Q1",
            "name": "Omid Example",
            "article_url": "https://en.wikipedia.org/wiki/Omid_Example",
            "language": "en",
            "resolved_title": "Omid Example",
            "revision_id": 1,
            "article_bytes": len(raw.encode()),
            "raw_wikitext": raw,
        }
        person = {
            "wikidata_id": "Q1",
            "name": "Omid Example",
            "member_classes": ["possible"],
            "age_status": "possible",
            "birth_date": "1997",
            "death_date": "2025",
            "source_row_sha256": "hash",
        }
        record = batch._scan_ambiguous_article(
            person, article, batch.build_packet(article)
        )
        self.assertEqual(record["canonical_infobox_age"], 27)
        infobox_births = {
            item["value"]
            for item in record["candidates"]
            if item.get("deterministic_claim_type") == "birth_date"
            and item["source"] == "infobox"
        }
        self.assertEqual(infobox_births, {"1997", "1998"})

    def test_body_age_resolves_ambiguous_infobox_range(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            person = {
                "wikidata_id": "Q1",
                "name": "Person",
                "age_status": "possible",
                "birth_date": "1997",
                "death_date": "2025",
                "member_classes": ["possible"],
                "source_row_sha256": "hash",
            }
            record = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "name": "Person",
                "article_url": "https://en.wikipedia.org/wiki/Person",
                "revision_id": 1,
                "member_classes": ["possible"],
                "age_status": "possible",
                "wikidata_birth_dates": ["1997"],
                "wikidata_death_dates": ["2025"],
                "source_row_sha256": "hash",
                "canonical_infobox_age": None,
                "ambiguous_infobox_ages": [27, 28],
                "semantic_candidate_ids": ["C002"],
                "candidates": [
                    {
                        "candidate_id": "C001",
                        "kind": "age_range",
                        "source": "infobox",
                        "value": "27-28",
                        "excerpt": "aged 27-28",
                        "requires_semantic_review": False,
                        "deterministic_claim_type": "other",
                        "reason": "range",
                    },
                    {
                        "candidate_id": "C002",
                        "kind": "age",
                        "source": "citation_title",
                        "value": 27,
                        "excerpt": "Actor dies at 27",
                        "requires_semantic_review": True,
                    },
                ],
            }
            batch.atomic_write_json(
                run_dir / "ambiguity" / "candidates" / "Q1.json", record
            )
            batch.atomic_write_json(
                run_dir / "ambiguity" / "semantic" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "candidate_reviews": [
                        {
                            "candidate_id": "C002",
                            "verdict": "confirmed",
                            "claim_type": "age_at_death",
                            "reason": "The title explicitly gives age at death.",
                        }
                    ],
                    "reason": "Citation title resolves the range.",
                },
            )
            row = batch._assemble_ambiguous_review_row(run_dir, person)
            self.assertEqual(row["confirmed_age_at_death"], "27")
            self.assertEqual(row["recommended_action"], "elevate")
            self.assertEqual(row["conflict"], "")

    def test_unresolved_membership_range_is_none_not_review(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            person = {
                "wikidata_id": "Q1",
                "name": "Person",
                "age_status": "possible",
                "birth_date": "1997",
                "death_date": "2025",
                "member_classes": ["possible"],
                "source_row_sha256": "hash",
            }
            record = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "name": "Person",
                "article_url": "https://en.wikipedia.org/wiki/Person",
                "revision_id": 1,
                "member_classes": ["possible"],
                "age_status": "possible",
                "wikidata_birth_dates": ["1997"],
                "wikidata_death_dates": ["2025"],
                "source_row_sha256": "hash",
                "canonical_infobox_age": None,
                "ambiguous_infobox_ages": [27, 28],
                "semantic_candidate_ids": [],
                "candidates": [{
                    "candidate_id": "C001",
                    "kind": "age_range",
                    "source": "infobox",
                    "value": "27-28",
                    "excerpt": "aged 27-28",
                    "requires_semantic_review": False,
                    "deterministic_claim_type": "other",
                    "reason": "range",
                }],
            }
            batch.atomic_write_json(
                run_dir / "ambiguity" / "candidates" / "Q1.json", record
            )
            row = batch._assemble_ambiguous_review_row(run_dir, person)
            self.assertEqual(row["recommended_action"], "none")
            self.assertEqual(row["confirmed_age_at_death"], "")

    def test_direct_age_action_is_not_blocked_by_unresolved_date_alternative(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            person = {
                "wikidata_id": "Q1",
                "name": "Person",
                "age_status": "possible",
                "birth_date": "1872",
                "death_date": "1899-09-24",
                "member_classes": ["possible"],
                "source_row_sha256": "hash",
            }
            record = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "name": "Person",
                "article_url": "https://en.wikipedia.org/wiki/Person",
                "revision_id": 1,
                "member_classes": ["possible"],
                "age_status": "possible",
                "wikidata_birth_dates": ["1872"],
                "wikidata_death_dates": ["1899-09-24"],
                "source_row_sha256": "hash",
                "canonical_infobox_age": None,
                "ambiguous_infobox_ages": [],
                "semantic_candidate_ids": ["C001"],
                "candidates": [
                    {"candidate_id":"C001","kind":"age","source":"article_body","value":27,"excerpt":"He died at age 27.","requires_semantic_review":True},
                    {"candidate_id":"C002","kind":"date","source":"infobox","value":"1871","excerpt":"1871 or 1872","requires_semantic_review":False,"deterministic_claim_type":"birth_date","reason":"date"},
                    {"candidate_id":"C003","kind":"date","source":"infobox","value":"1872","excerpt":"1871 or 1872","requires_semantic_review":False,"deterministic_claim_type":"birth_date","reason":"date"},
                ],
            }
            batch.atomic_write_json(run_dir / "ambiguity" / "candidates" / "Q1.json", record)
            batch.atomic_write_json(run_dir / "ambiguity" / "semantic" / "Q1.json", {
                "schema_version":1,"wikidata_id":"Q1","candidate_reviews":[
                    {"candidate_id":"C001","verdict":"confirmed","claim_type":"age_at_death","reason":"explicit"}
                ],"reason":"explicit age"
            })
            row = batch._assemble_ambiguous_review_row(run_dir, person)
            self.assertEqual(row["recommended_action"], "elevate")
            self.assertIn("conflicting equal-precision", row["conflict"])

    def test_conflicting_singular_prose_and_exact_date_ages_require_review(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            person = {
                "wikidata_id":"Q1","name":"Person","age_status":"possible",
                "birth_date":"1907","death_date":"1934","member_classes":["possible"],
                "source_row_sha256":"hash",
            }
            record = {
                "schema_version":1,"wikidata_id":"Q1","name":"Person",
                "article_url":"https://en.wikipedia.org/wiki/Person","revision_id":1,
                "member_classes":["possible"],"age_status":"possible",
                "wikidata_birth_dates":["1907"],"wikidata_death_dates":["1934"],
                "source_row_sha256":"hash","canonical_infobox_age":None,
                "ambiguous_infobox_ages":[],"semantic_candidate_ids":["C001","C002","C003"],
                "candidates":[
                    {"candidate_id":"C001","kind":"age","source":"article_body","value":27,"excerpt":"He died at age 27.","requires_semantic_review":True},
                    {"candidate_id":"C002","kind":"date","source":"lead","value":"1907-05-15","excerpt":"15 May 1907 - 14 May 1934","requires_semantic_review":True},
                    {"candidate_id":"C003","kind":"date","source":"lead","value":"1934-05-14","excerpt":"15 May 1907 - 14 May 1934","requires_semantic_review":True},
                ],
            }
            batch.atomic_write_json(run_dir / "ambiguity" / "candidates" / "Q1.json", record)
            batch.atomic_write_json(run_dir / "ambiguity" / "semantic" / "Q1.json", {
                "schema_version":1,"wikidata_id":"Q1","candidate_reviews":[
                    {"candidate_id":"C001","verdict":"confirmed","claim_type":"age_at_death","reason":"explicit"},
                    {"candidate_id":"C002","verdict":"confirmed","claim_type":"birth_date","reason":"explicit"},
                    {"candidate_id":"C003","verdict":"confirmed","claim_type":"death_date","reason":"explicit"},
                ],"reason":"all explicit"
            })
            row = batch._assemble_ambiguous_review_row(run_dir, person)
            self.assertEqual(row["confirmed_age_at_death"], "")
            self.assertEqual(row["recommended_action"], "review")
            self.assertIn("singular age-at-death candidates: 26, 27", row["conflict"])

    def test_conflicting_infobox_and_body_singular_ages_require_review(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            person = {
                "wikidata_id": "Q1",
                "name": "Alex Person",
                "age_status": "possible",
                "birth_date": "1995",
                "death_date": "2022",
                "member_classes": ["possible"],
                "source_row_sha256": "hash",
            }
            record = {
                "schema_version": 1,
                "wikidata_id": "Q1",
                "name": "Alex Person",
                "article_url": "https://en.wikipedia.org/wiki/Alex_Person",
                "revision_id": 1,
                "member_classes": ["possible"],
                "age_status": "possible",
                "wikidata_birth_dates": ["1995"],
                "wikidata_death_dates": ["2022"],
                "source_row_sha256": "hash",
                "canonical_infobox_age": 27,
                "ambiguous_infobox_ages": [],
                "semantic_candidate_ids": ["C002"],
                "candidates": [
                    {
                        "candidate_id": "C001",
                        "kind": "age",
                        "source": "infobox",
                        "value": 27,
                        "excerpt": "died (aged 27)",
                        "requires_semantic_review": False,
                        "deterministic_claim_type": "age_at_death",
                        "reason": "explicit",
                    },
                    {
                        "candidate_id": "C002",
                        "kind": "age",
                        "source": "article_body",
                        "value": 28,
                        "excerpt": "The article says he was 28.",
                        "requires_semantic_review": True,
                    },
                ],
            }
            batch.atomic_write_json(
                run_dir / "ambiguity" / "candidates" / "Q1.json", record
            )
            batch.atomic_write_json(
                run_dir / "ambiguity" / "semantic" / "Q1.json",
                {
                    "schema_version": 1,
                    "wikidata_id": "Q1",
                    "candidate_reviews": [
                        {
                            "candidate_id": "C002",
                            "verdict": "confirmed",
                            "claim_type": "age_at_death",
                            "reason": "The body makes that claim.",
                        }
                    ],
                    "reason": "The body claim is explicit.",
                },
            )
            row = batch._assemble_ambiguous_review_row(run_dir, person)
            self.assertEqual(row["confirmed_age_at_death"], "")
            self.assertEqual(row["recommended_action"], "review")
            self.assertIn("singular age-at-death candidates: 27, 28", row["conflict"])

    def test_deterministic_tranche_finalizes_to_report_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            state = root / "state.json"
            report = root / "review.csv"
            run_dir = root / "run"
            row = blank_row("Q1", "Alex Person", "2022")
            row["age_status"] = "possible"
            row["birth_date"] = "1995"
            write_csv(people, TEST_COLUMNS + ["age_status"], [row])
            batch.create_ambiguous_cohort(
                people_csv=people,
                cache_root=cache,
                state_path=state,
                batch_size=None,
                run_dir=run_dir,
            )
            raw = (
                "{{Infobox person\n"
                "| birth_date = {{Birth date|1995|7|14}}\n"
                "| death_date = {{Death date and age|2022|7|26|1995|7|14}}\n"
                "}}\n'''Alex Person''' was an actor."
            )
            write_article_and_packet(cache, run_dir, "Q1", "Alex Person", raw)
            batch.scan_ambiguous_members(cohort_value=run_dir, cache_root=cache)
            status = batch.ambiguous_scheduler_status(cohort_value=run_dir)
            self.assertTrue(status["tranches"][0]["reviewable"])
            result = batch.finalize_ambiguous_tranche(
                cohort_value=run_dir,
                tranche=1,
                review_csv=report,
                state_path=state,
            )
            self.assertEqual(result["report_rows"], 1)
            _, report_rows = batch.read_csv(report)
            self.assertEqual(report_rows[0]["recommended_action"], "elevate")
            saved_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(saved_state["members"]["Q1"]["outcome"], "reported")

    def test_refillable_semantic_assignment_validates_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people = root / "people.csv"
            cache = root / "cache"
            run_dir = root / "run"
            row = blank_row("Q1", "Alex Person", "2022")
            row["age_status"] = "possible"
            write_csv(people, TEST_COLUMNS + ["age_status"], [row])
            batch.create_ambiguous_cohort(
                people_csv=people,
                cache_root=cache,
                state_path=root / "state.json",
                batch_size=None,
                run_dir=run_dir,
            )
            write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "Alex Person",
                "'''Alex Person''' was an actor.\n\n==Death==\nHe died at 27.",
            )
            batch.scan_ambiguous_members(cohort_value=run_dir, cache_root=cache)
            with self.assertRaisesRegex(batch.BatchError, "Slot must be 1..6"):
                batch.claim_ambiguous_assignment(cohort_value=run_dir, slot=7)
            assignment = batch.claim_ambiguous_assignment(
                cohort_value=run_dir, slot=6
            )
            self.assertEqual(assignment["role"], "ambiguous-member-review")
            role_input = json.loads(
                Path(assignment["items"][0]["input"]).read_text(encoding="utf-8")
            )
            candidate_id = role_input["candidates"][0]["candidate_id"]
            output = root / "result.json"
            batch.atomic_write_json(
                output,
                [
                    {
                        "schema_version": 1,
                        "wikidata_id": "Q1",
                        "candidate_reviews": [
                            {
                                "candidate_id": candidate_id,
                                "verdict": "confirmed",
                                "claim_type": "age_at_death",
                                "reason": "The sentence explicitly states age at death.",
                            }
                        ],
                        "reason": "The only candidate is explicit.",
                    }
                ],
            )
            completed = batch.complete_ambiguous_assignment(
                cohort_value=run_dir,
                assignment_id=assignment["assignment_id"],
                input_path=output,
            )
            self.assertEqual(completed["installed"], ["Q1"])
            self.assertEqual(completed["slot_released"], 6)
            status = batch.ambiguous_scheduler_status(cohort_value=run_dir)
            self.assertEqual(status["active_leases"], {})
            self.assertTrue(status["tranches"][0]["reviewable"])


if __name__ == "__main__":
    unittest.main()
