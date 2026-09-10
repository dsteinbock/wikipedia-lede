from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from wikidata_age27 import DataValidationError


MODULE_PATH = Path(__file__).resolve().parents[1] / "refresh_descriptions.py"
SPEC = importlib.util.spec_from_file_location("age27_people_refresh_descriptions", MODULE_PATH)
assert SPEC and SPEC.loader
refresh_descriptions = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh_descriptions
SPEC.loader.exec_module(refresh_descriptions)


class DescriptionRefreshTests(unittest.TestCase):
    def test_fetches_english_descriptions_for_each_qid(self):
        class Client:
            def query(self, query):
                self.query_text = query
                return {
                    "data": {
                        "itemsById": [
                            {"id": "Q1", "description": "English description"},
                            {"id": "Q2", "description": None},
                        ]
                    }
                }

        client = Client()
        descriptions = refresh_descriptions.fetch_descriptions(client, ["Q1", "Q2"])
        self.assertIn('description(languageCode: "en")', client.query_text)
        self.assertEqual(descriptions, {"Q1": "English description", "Q2": ""})

    def test_missing_qid_is_rejected(self):
        class Client:
            def query(self, query):
                return {"data": {"itemsById": [{"id": "Q1", "description": "one"}]}}

        with self.assertRaises(DataValidationError):
            refresh_descriptions.fetch_descriptions(Client(), ["Q1", "Q2"])

    def test_legacy_csv_schema_is_upgraded_with_description_column(self):
        legacy_columns = [column for column in refresh_descriptions.CSV_COLUMNS if column != "description"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "people.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=legacy_columns)
                writer.writeheader()
                writer.writerow({column: "Q1" if column == "wikidata_id" else "" for column in legacy_columns})
            rows = refresh_descriptions.read_rows(path)
            rows[0]["description"] = "A person"
            refresh_descriptions.write_rows(path, rows)
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, refresh_descriptions.CSV_COLUMNS)
                self.assertEqual(next(reader)["description"], "A person")

    def test_normalizes_trailing_lifespan_and_death_year_without_erasing_other_dates(self):
        normalize = refresh_descriptions.normalize_description
        self.assertEqual(
            normalize("Ugandan footballer (1998–2026).", "1998", "2026-01-05"),
            "Ugandan footballer",
        )
        self.assertEqual(
            normalize("English politician, died 1606", "1578", "1606"),
            "English politician",
        )
        self.assertEqual(
            normalize("14th Sultan of the Ottoman Empire (1603–1617)", "1590", "1617"),
            "14th Sultan of the Ottoman Empire (1603–1617)",
        )
        self.assertEqual(
            normalize("Landowner in Devon.", "1800", "1827"),
            "Landowner in Devon",
        )
