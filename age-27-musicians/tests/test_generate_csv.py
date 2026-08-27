from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import requests


MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_csv.py"
SPEC = importlib.util.spec_from_file_location("age27_generate_csv", MODULE_PATH)
assert SPEC and SPEC.loader
age27 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = age27
SPEC.loader.exec_module(age27)


def structured(value: str, precision: int = 11, calendar: str = age27.GREGORIAN):
    return age27.StructuredTime(value, precision, calendar)


def exact(year: int, month: int, day: int, calendar: str = age27.GREGORIAN):
    sign = "+" if year >= 0 else "-"
    return structured(
        f"{sign}{abs(year):04d}-{month:02d}-{day:02d}T00:00:00Z",
        11,
        calendar,
    )


class DateMathTests(unittest.TestCase):
    def test_bessie_tucker(self):
        result = age27.calculate_age_range(
            [structured("+1906-01-01T00:00:00Z", 9)],
            [exact(1933, 1, 6)],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "possible")
        self.assertEqual(result.minimum_days, 9503)
        self.assertEqual(result.maximum_days, 9867)
        self.assertEqual(result.display(), "26 years, 6 days to 27 years, 5 days")

    def test_exact_age_is_confirmed(self):
        result = age27.calculate_age_range([exact(1970, 5, 1)], [exact(1997, 6, 2)])
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.display(), "27 years, 32 days")

    def test_wdqs_unsigned_positive_year(self):
        value = structured("1838-01-08T00:00:00Z")
        self.assertEqual(value.components(), (1838, 1, 8))
        self.assertEqual(value.display(), "1838-01-08")

    def test_human_age_uses_singular_units(self):
        self.assertEqual(age27.format_calendar_age(1, 1), "1 year, 1 day")

    def test_month_precision_can_be_possible(self):
        result = age27.calculate_age_range(
            [structured("+1900-06-01T00:00:00Z", 10)],
            [exact(1927, 6, 15)],
        )
        self.assertEqual(result.status, "possible")
        self.assertEqual(result.minimum_age[0], 26)
        self.assertEqual(result.maximum_age[0], 27)

    def test_multiple_best_ranked_statements_expand_range(self):
        result = age27.calculate_age_range(
            [exact(1900, 1, 1), exact(1900, 12, 31)],
            [exact(1927, 6, 1)],
        )
        self.assertEqual(result.status, "possible")
        self.assertEqual(result.minimum_age[0], 26)
        self.assertEqual(result.maximum_age[0], 27)

    def test_wider_range_is_excluded(self):
        result = age27.calculate_age_range(
            [exact(1899, 1, 1), exact(1903, 1, 1)],
            [exact(1927, 1, 1)],
        )
        self.assertIsNone(result)

    def test_invalid_chronology_raises(self):
        with self.assertRaises(age27.DataValidationError):
            age27.calculate_age_range([exact(2000, 1, 1)], [exact(1999, 1, 1)])

    def test_coarse_precision_is_rejected(self):
        with self.assertRaises(age27.DataValidationError):
            structured("+1900-01-01T00:00:00Z", 8).bounds()

    def test_gregorian_and_julian_leap_rules(self):
        self.assertEqual(age27.days_in_month(1900, 2, age27.GREGORIAN), 28)
        self.assertEqual(age27.days_in_month(1900, 2, age27.JULIAN), 29)
        result = age27.calculate_age_range(
            [exact(1900, 2, 29, age27.JULIAN)],
            [exact(1927, 2, 28, age27.JULIAN)],
        )
        self.assertEqual(result.status, "confirmed")


class OccupationTests(unittest.TestCase):
    def setUp(self):
        self.singer = "Q_SINGER"
        self.composer = "Q_COMPOSER"
        self.club_dj = "Q_CLUB_DJ"
        self.record_producer = "Q_RECORD_PRODUCER"
        self.educator = "Q_EDUCATOR"
        self.ancestry = {
            age27.MUSICIAN: {age27.MUSICIAN},
            self.singer: {self.singer, age27.MUSICIAN},
            self.composer: {self.composer, age27.MUSICIAN},
            age27.GENERIC_DISC_JOCKEY: {
                age27.GENERIC_DISC_JOCKEY,
                age27.MUSICIAN,
            },
            self.club_dj: {
                self.club_dj,
                age27.GENERIC_DISC_JOCKEY,
                age27.MUSICIAN,
            },
            self.record_producer: {self.record_producer, age27.MUSICIAN},
            self.educator: {self.educator, "Q16145150", age27.MUSICIAN},
        }

    def test_generic_dj_only_is_excluded(self):
        self.assertEqual(
            age27.qualifying_occupations({age27.GENERIC_DISC_JOCKEY}, self.ancestry),
            set(),
        )

    def test_performance_dj_and_record_producer_are_included(self):
        result = age27.qualifying_occupations(
            {self.club_dj, self.record_producer}, self.ancestry
        )
        self.assertEqual(result, {self.club_dj, self.record_producer})

    def test_blocked_occupation_does_not_block_separate_singer(self):
        result = age27.qualifying_occupations({self.educator, self.singer}, self.ancestry)
        self.assertEqual(result, {self.singer})

    def test_leaf_reduction(self):
        leaves = age27.leaf_occupations(
            {age27.MUSICIAN, self.singer, self.composer}, self.ancestry
        )
        self.assertEqual(leaves, {self.singer, self.composer})


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {"results": {"bindings": []}}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ClientTests(unittest.TestCase):
    def test_detail_query_requests_best_ranked_cause_and_manner(self):
        query = age27.detail_query(["Q1"])
        self.assertIn("p:P509", query)
        self.assertIn("ps:P509", query)
        self.assertIn("p:P1196", query)
        self.assertIn("ps:P1196", query)
        self.assertEqual(query.count("STRSTARTS"), 2)
        self.assertEqual(query.count("wikibase:BestRank"), 5)

    def test_fetch_people_collects_death_labels_and_qid_fallback(self):
        class Client:
            def query(self, query):
                return {
                    "results": {
                        "bindings": [
                            {
                                "person": {"value": "http://www.wikidata.org/entity/Q1"},
                                "personLabel": {"value": "Example"},
                                "article": {"value": "https://en.wikipedia.org/wiki/Example"},
                                "birthTime": {"value": "+1970-01-01T00:00:00Z"},
                                "birthPrecision": {"value": "11"},
                                "birthCalendar": {"value": "http://www.wikidata.org/entity/Q1985727"},
                                "deathTime": {"value": "+1997-01-02T00:00:00Z"},
                                "deathPrecision": {"value": "11"},
                                "deathCalendar": {"value": "http://www.wikidata.org/entity/Q1985727"},
                                "occupation": {"value": "http://www.wikidata.org/entity/Q_SINGER"},
                                "cause": {"value": "http://www.wikidata.org/entity/Q10"},
                                "causeLabel": {"value": "Example cause"},
                                "manner": {"value": "http://www.wikidata.org/entity/Q20"},
                            }
                        ]
                    }
                }

        person = age27.fetch_people(Client(), ["Q1"])["Q1"]
        self.assertEqual(person.causes_of_death, {"Example cause"})
        self.assertEqual(person.manners_of_death, {"Q20"})

    def test_non_item_death_value_is_ignored(self):
        binding = {
            "cause": {
                "value": "http://www.wikidata.org/.well-known/genid/751029b19e34f076"
            }
        }
        self.assertIsNone(age27._item_label(binding, "cause", "causeLabel"))

    def test_successful_response_is_cached(self):
        payload = {"results": {"bindings": [{"x": {"value": "ok"}}]}}
        session = FakeSession([FakeResponse(payload=payload)])
        with tempfile.TemporaryDirectory() as directory:
            client = age27.WDQSClient(Path(directory), session=session, sleep=lambda _: None)
            self.assertEqual(client.query("SELECT * WHERE {}"), payload)
            self.assertEqual(client.query("SELECT * WHERE {}"), payload)
        self.assertEqual(session.calls, 1)

    def test_retry_after_is_honored_for_429(self):
        sleeps = []
        session = FakeSession(
            [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse()]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = age27.WDQSClient(Path(directory), session=session, sleep=sleeps.append)
            client.query("SELECT ?x WHERE {}")
        self.assertIn(7.0, sleeps)
        self.assertEqual(session.calls, 2)

    def test_503_without_header_uses_backoff(self):
        sleeps = []
        session = FakeSession([FakeResponse(503), FakeResponse()])
        with tempfile.TemporaryDirectory() as directory:
            client = age27.WDQSClient(Path(directory), session=session, sleep=sleeps.append)
            client.query("SELECT ?y WHERE {}")
        self.assertIn(5, sleeps)

    def test_timeout_partitions_musician_query(self):
        class PartitioningClient:
            def __init__(self):
                self.queries = []

            def query(self, query):
                self.queries.append(query)
                if "wd:Q639669" in query and "YEAR(?death) < 1800" not in query and "YEAR(?death) >=" not in query:
                    raise age27.QueryTimeout("timeout")
                qid = "Q1" if "wd:Q639669" in query else "Q2"
                return {
                    "results": {
                        "bindings": [
                            {"person": {"value": f"http://www.wikidata.org/entity/{qid}"}}
                        ]
                    }
                }

        client = PartitioningClient()
        self.assertEqual(age27.discover_candidate_ids(client), ["Q1", "Q2"])
        self.assertEqual(len(client.queries), 9)


class OutputTests(unittest.TestCase):
    def test_rows_have_exact_schema_and_ascending_death_date_order(self):
        people = {
            "Q2": age27.RawPerson(
                qid="Q2",
                name="Zulu",
                wikipedia_url="https://en.wikipedia.org/wiki/Zulu",
                births={exact(1970, 1, 1)},
                deaths={exact(1997, 2, 1)},
                occupations={"Q_SINGER"},
            ),
            "Q1": age27.RawPerson(
                qid="Q1",
                name="Alpha",
                wikipedia_url="https://en.wikipedia.org/wiki/Alpha",
                births={structured("+1970-01-01T00:00:00Z", 9)},
                deaths={exact(1997, 1, 6)},
                occupations={"Q_SINGER"},
                causes_of_death={"zeta", "Alpha"},
                manners_of_death={"accident"},
            ),
        }
        ancestry = {"Q_SINGER": {"Q_SINGER", age27.MUSICIAN}}
        rows = age27.build_rows(people, ancestry, {"Q_SINGER": "singer"})
        age27.validate_rows(rows)
        self.assertEqual([row["wikidata_id"] for row in rows], ["Q1", "Q2"])
        self.assertEqual(rows[0]["cause_of_death"], "Alpha; zeta")
        self.assertEqual(rows[0]["manner_of_death"], "accident")
        self.assertEqual(rows[1]["cause_of_death"], "")
        self.assertEqual(rows[1]["manner_of_death"], "")
        self.assertEqual(list(rows[0].keys()), age27.CSV_COLUMNS)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.csv"
            age27.write_csv(rows, output)
            with output.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(next(csv.reader(handle)), age27.CSV_COLUMNS)


if __name__ == "__main__":
    unittest.main()
