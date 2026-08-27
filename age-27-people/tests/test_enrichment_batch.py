import csv
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
                "infobox",
                "rest_of_article",
            )
        )
        self.assertIn("Coroner reports an overdose", combined)
        self.assertIn("The death was accidental", combined)
        self.assertIn("painter", packet["infobox"])
        self.assertIn(marker, packet["rest_of_article"])
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(packet["article_sections"][0]["heading"], "Death")
        self.assertIn("motorcycle", packet["article_sections"][0]["text"])
        self.assertTrue(packet["death_evidence_candidates"])
        self.assertTrue(
            any(
                "motorcycle" in candidate["excerpt"]
                for candidate in packet["death_evidence_candidates"]
            )
        )

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
                root / "unresolved_vocabulary.json", ["gunshot wound"]
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
            self.assertEqual(
                candidates["labels"]["gunshot wound"][0]["id"], "Q2140674"
            )


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

    def test_validation_rejects_unknown_without_every_candidate_disposition(self):
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
            packet = write_article_and_packet(
                cache,
                run_dir,
                "Q1",
                "Person",
                "'''Person''' was an actor.\n\n==Death==\n"
                "Person died after an illness, but no manner was reported.",
            )
            self.assertTrue(packet["death_evidence_candidates"])
            proposals_path = batch.init_proposals(cohort_value=run_dir)
            proposals = batch.load_proposals(proposals_path)
            proposals[0].update(
                {
                    "article_eligibility": primary_eligible(),
                    "manner": [{"label": "somevalue", "qid": ""}],
                    "status": "unknown",
                    "evidence_basis": {
                        "cause": None,
                        "manner": evidence(
                            "none", "", "", "No manner is established."
                        ),
                        "occupation": None,
                    },
                    "unknown_review": {
                        "cause": None,
                        "manner": {
                            "reviewed_source_tiers": batch.DEATH_REVIEW_TIERS,
                            "candidate_dispositions": {},
                            "conclusion": "No manner is established.",
                        },
                    },
                }
            )
            batch.write_proposals(proposals_path, proposals)
            with self.assertRaisesRegex(batch.BatchError, "candidate audit mismatch"):
                batch.validate_proposals(
                    cohort_value=run_dir,
                    proposals_path=proposals_path,
                    people_csv=people,
                    cache_root=cache,
                )

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


class ArticleEligibilityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
