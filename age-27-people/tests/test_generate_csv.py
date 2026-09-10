from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import requests

from wikidata_age27 import (
    GREGORIAN,
    GraphQLClient,
    QueryTimeout,
    RawPerson,
    StructuredTime,
)


MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_csv.py"
SPEC = importlib.util.spec_from_file_location("age27_people_generate_csv", MODULE_PATH)
assert SPEC and SPEC.loader
people27 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = people27
SPEC.loader.exec_module(people27)


def exact(year: int, month: int, day: int) -> StructuredTime:
    return StructuredTime(f"+{year:04d}-{month:02d}-{day:02d}T00:00:00Z", 11, GREGORIAN)


def payload(qids):
    bindings = []
    for qid in qids:
        bindings.append(
            {
                "person": {"value": f"http://www.wikidata.org/entity/{qid}"},
                "birthTime": {"value": "+1970-01-01T00:00:00Z"},
                "birthPrecision": {"value": "11"},
                "birthCalendar": {"value": "http://www.wikidata.org/entity/Q1985727"},
                "deathTime": {"value": "+1997-02-01T00:00:00Z"},
                "deathPrecision": {"value": "11"},
                "deathCalendar": {"value": "http://www.wikidata.org/entity/Q1985727"},
            }
        )
    return {"results": {"bindings": bindings}}


class ShardTests(unittest.TestCase):
    def test_query_uses_indexed_bounds_subquery_and_range_hint(self):
        query = people27.shard_query(1900, 1950)
        self.assertIn("WITH {", query)
        self.assertIn("AS %candidatePeople", query)
        self.assertIn("INCLUDE %candidatePeople", query)
        self.assertIn("hint:rangeSafe true", query)
        self.assertIn('>= "1900-01-01', query)
        self.assertIn('< "1950-01-01', query)

    def test_timeout_bisects_and_deduplicates_boundary_items(self):
        class Client:
            def __init__(self):
                self.calls = []

            def query(self, query):
                self.calls.append(query)
                if '>= "1900-01-01' in query and '< "1904-01-01' in query:
                    raise QueryTimeout("timeout")
                return payload(["Q1"])

        client = Client()
        result = people27.fetch_shard(client, 1900, 1904)
        self.assertEqual(set(result), {"Q1"})
        self.assertEqual(len(client.calls), 3)

    def test_irreducible_one_year_timeout_fails(self):
        class Client:
            def query(self, query):
                raise QueryTimeout("timeout")

        with self.assertRaises(QueryTimeout):
            people27.fetch_shard(Client(), 1900, 1901)

    def test_open_historical_split_points(self):
        self.assertEqual(
            people27._split_shard(None, 1800), ((None, 1000), (1000, 1800))
        )
        self.assertEqual(people27._split_shard(None, 1000), ((None, 0), (0, 1000)))


class EnrichmentTests(unittest.TestCase):
    def test_batches_are_limited_to_fifty(self):
        queries = []

        class Client:
            def query(self, query):
                queries.append(query)
                ids = [token for token in query.replace('"', " ").split() if token.startswith("Q")]
                items = [
                    {
                        "id": qid.rstrip(","),
                        "label": qid.rstrip(","),
                        "sitelink": {
                            "title": qid.rstrip(","),
                            "url": f"https://en.wikipedia.org/wiki/{qid.rstrip(',')}",
                        },
                        "occupations": [],
                    }
                    for qid in ids
                ]
                return {"data": {"itemsById": items}}

        people = {f"Q{i}": RawPerson(f"Q{i}") for i in range(51)}
        people27.enrich_people(Client(), people)
        self.assertEqual(len(queries), 2)

    def test_preferred_occupations_replace_normal_and_qid_is_label_fallback(self):
        statements = [
            {"rank": "NORMAL", "value": {"id": "Q1", "label": "normal job"}},
            {"rank": "PREFERRED", "value": {"id": "Q2", "label": None}},
            {"rank": "DEPRECATED", "value": {"id": "Q3", "label": "old job"}},
        ]
        ids, labels = people27._best_occupation_labels(statements)
        self.assertEqual(ids, {"Q2"})
        self.assertEqual(labels, {"Q2"})

    def test_no_occupation_is_allowed(self):
        self.assertEqual(people27._best_occupation_labels([]), (set(), set()))

    def test_death_details_use_best_rank_labels_and_qid_fallback(self):
        class Client:
            def query(self, query):
                self.assertions(query)
                return {
                    "data": {
                        "itemsById": [
                            {
                                "id": "Q1",
                                "label": "Example",
                                "sitelink": {
                                    "title": "Example",
                                    "url": "https://en.wikipedia.org/wiki/Example",
                                },
                                "occupations": [],
                                "causesOfDeath": [
                                    {"rank": "NORMAL", "value": {"id": "Q10", "label": "old cause"}},
                                    {"rank": "PREFERRED", "value": {"id": "Q11", "label": "Zeta cause"}},
                                    {"rank": "PREFERRED", "value": {"id": "Q12", "label": "Alpha cause"}},
                                    {"rank": "DEPRECATED", "value": {"id": "Q13", "label": "discarded"}},
                                ],
                                "mannersOfDeath": [
                                    {"rank": "NORMAL", "value": {"id": "Q20", "label": None}}
                                ],
                            }
                        ]
                    }
                }

            @staticmethod
            def assertions(query):
                assert 'description(languageCode: "en")' in query
                assert 'statements(propertyId: "P509")' in query
                assert 'statements(propertyId: "P1196")' in query

        people = {"Q1": RawPerson("Q1")}
        people27.enrich_people(Client(), people)
        self.assertEqual(people["Q1"].description, "")
        self.assertEqual(people["Q1"].causes_of_death, {"Alpha cause", "Zeta cause"})
        self.assertEqual(people["Q1"].manners_of_death, {"Q20"})


class GraphQLClientTests(unittest.TestCase):
    class Response:
        def __init__(self, status=200, payload=None, headers=None):
            self.status_code = status
            self._payload = payload or {"data": {"itemsById": []}}
            self.headers = headers or {}
            self.text = ""

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    class Session:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return self.responses.pop(0)

    def test_successful_batch_is_cached_for_resume(self):
        response = self.Response(payload={"data": {"itemsById": [{"id": "Q1"}]}})
        session = self.Session([response])
        with tempfile.TemporaryDirectory() as directory:
            client = GraphQLClient(Path(directory), session=session, sleep=lambda _: None)
            self.assertEqual(client.query("query { item(id: \"Q1\") { id } }"), response._payload)
            self.assertEqual(client.query("query { item(id: \"Q1\") { id } }"), response._payload)
        self.assertEqual(session.calls, 1)

    def test_retry_after_is_honored(self):
        sleeps = []
        session = self.Session(
            [self.Response(503, headers={"Retry-After": "4"}), self.Response()]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = GraphQLClient(Path(directory), session=session, sleep=sleeps.append)
            client.query("query { item(id: \"Q2\") { id } }")
        self.assertIn(4.0, sleeps)
        self.assertEqual(session.calls, 2)

    def test_http_200_api_error_is_retried_and_not_treated_as_data(self):
        sleeps = []
        session = self.Session(
            [
                self.Response(payload={"error": {"code": "maxlag"}}),
                self.Response(payload={"data": {"itemsById": [{"id": "Q1"}]}}),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = GraphQLClient(Path(directory), session=session, sleep=sleeps.append)
            result = client.query('query { item(id: "Q1") { id } }')
        self.assertEqual(result["data"]["itemsById"][0]["id"], "Q1")
        self.assertEqual(session.calls, 2)
        self.assertIn(5, sleeps)


class OutputTests(unittest.TestCase):
    def test_bessie_and_deterministic_death_order(self):
        bessie = RawPerson(
            "Q3509314",
            name="Bessie Tucker",
            wikipedia_url="https://en.wikipedia.org/wiki/Bessie_Tucker",
            births={StructuredTime("+1906-01-01T00:00:00Z", 9, GREGORIAN)},
            deaths={exact(1933, 1, 6)},
            causes_of_death={"zeta", "Alpha"},
            manners_of_death={"accident"},
        )
        later = RawPerson(
            "Q2",
            name="Alpha",
            wikipedia_url="https://en.wikipedia.org/wiki/Alpha",
            births={exact(1970, 1, 1)},
            deaths={exact(1997, 2, 1)},
        )
        rows, exclusions = people27.build_rows(
            {"Q2": later, "Q3509314": bessie}, {"Q3509314": {"singer"}, "Q2": set()}
        )
        people27.validate_rows(rows)
        self.assertFalse(exclusions)
        self.assertEqual([row["wikidata_id"] for row in rows], ["Q3509314", "Q2"])
        row = rows[0]
        self.assertEqual(row["age_status"], "possible")
        self.assertEqual(row["minimum_lifespan_days"], 9503)
        self.assertEqual(row["maximum_lifespan_days"], 9867)
        self.assertEqual(row["possible_age_range"], "26 years, 6 days to 27 years, 5 days")
        self.assertEqual(row["cause_of_death"], "Alpha; zeta")
        self.assertEqual(row["manner_of_death"], "accident")
        self.assertEqual(list(row), people27.CSV_COLUMNS)

    def test_wikipedia_fallbacks_are_preserved_by_qid(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "people.csv"
            with output.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=people27.CSV_COLUMNS)
                writer.writeheader()
                writer.writerow({
                    **{column: "" for column in people27.CSV_COLUMNS},
                    "wikidata_id": "Q1",
                    "wikipedia_cause_of_death": "gunshot wound",
                    "wikipedia_cause_of_death_qids": "Q2140674",
                    "wikipedia_death_review_status": "disputed",
                })
            rows = [
                {column: "" for column in people27.CSV_COLUMNS},
                {column: "" for column in people27.CSV_COLUMNS},
            ]
            rows[0]["wikidata_id"] = "Q1"
            rows[1]["wikidata_id"] = "Q2"
            people27.apply_wikipedia_fallbacks(
                rows, people27.load_wikipedia_fallbacks(output)
            )
        self.assertEqual(rows[0]["wikipedia_cause_of_death"], "gunshot wound")
        self.assertEqual(rows[0]["wikipedia_death_review_status"], "disputed")
        self.assertEqual(rows[1]["wikipedia_cause_of_death"], "")

    def test_removed_entries_are_excluded_from_future_rows(self):
        rows = [
            {column: "" for column in people27.CSV_COLUMNS},
            {column: "" for column in people27.CSV_COLUMNS},
        ]
        rows[0]["wikidata_id"] = "Q1"
        rows[1]["wikidata_id"] = "Q2"
        kept, removed = people27.exclude_removed_rows(rows, {"Q1"})
        self.assertEqual(removed, 1)
        self.assertEqual([row["wikidata_id"] for row in kept], ["Q2"])

    def test_removed_entry_ledger_requires_permanent_audit_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "removed_entries.csv"
            row = {column: "" for column in people27.REMOVED_ENTRY_COLUMNS}
            row.update(
                {
                    "wikidata_id": "Q1",
                    "removal_reason": "living",
                    "removed_utc": "2026-08-28T00:00:00+00:00",
                    "source_run_dir": "/tmp/run",
                }
            )
            with ledger.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=people27.REMOVED_ENTRY_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(people27.load_removed_qids(ledger), {"Q1"})

    def test_wikipedia_pairs_validate_order_alignment_somevalue_and_status(self):
        person = RawPerson(
            "Q1",
            name="Example",
            wikipedia_url="https://en.wikipedia.org/wiki/Example",
            births={exact(1970, 1, 1)},
            deaths={exact(1997, 2, 1)},
        )
        rows, _ = people27.build_rows({"Q1": person}, {"Q1": set()})
        row = rows[0]
        row["wikipedia_cause_of_death"] = "somevalue"
        row["wikipedia_death_review_status"] = "unknown"
        people27.validate_rows(rows)
        row["wikipedia_death_review_status"] = "possible_removal"
        people27.validate_rows(rows)

        row["wikipedia_occupations"] = "Zeta; Alpha"
        row["wikipedia_occupation_qids"] = "Q2; Q1"
        with self.assertRaisesRegex(Exception, "Unsorted"):
            people27.validate_rows(rows)

        row["wikipedia_occupations"] = "Alpha; Zeta"
        row["wikipedia_occupation_qids"] = "Q1"
        with self.assertRaisesRegex(Exception, "Unaligned"):
            people27.validate_rows(rows)

    def test_exact_wikipedia_fallback_column_order(self):
        self.assertEqual(
            people27.CSV_COLUMNS[-7:],
            [
                "wikipedia_cause_of_death",
                "wikipedia_cause_of_death_qids",
                "wikipedia_manner_of_death",
                "wikipedia_manner_of_death_qids",
                "wikipedia_occupations",
                "wikipedia_occupation_qids",
                "wikipedia_death_review_status",
            ],
        )

    def test_wide_and_invalid_candidates_get_exclusion_reasons(self):
        wide = RawPerson(
            "Q1",
            births={exact(1899, 1, 1), exact(1903, 1, 1)},
            deaths={exact(1927, 1, 1)},
        )
        invalid = RawPerson("Q2", births={exact(2000, 1, 1)}, deaths={exact(1999, 1, 1)})
        rows, exclusions = people27.build_rows({"Q1": wide, "Q2": invalid}, {})
        self.assertEqual(rows, [])
        self.assertEqual(exclusions["outside_age_rule"], 1)
        self.assertEqual(exclusions["invalid_chronology"], 1)


if __name__ == "__main__":
    unittest.main()
