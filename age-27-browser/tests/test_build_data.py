from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_data.py"
SPEC = importlib.util.spec_from_file_location("age27_browser_build_data", MODULE_PATH)
assert SPEC and SPEC.loader
build_data = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_data
SPEC.loader.exec_module(build_data)


class PayloadTests(unittest.TestCase):
    def test_load_compacts_populated_and_blank_death_details(self):
        fieldnames = [
            "name", "wikipedia_url", "wikidata_id", "birth_date", "death_date",
            "cause_of_death", "manner_of_death", "age_status",
            "minimum_lifespan_days", "maximum_lifespan_days",
            "possible_age_range", "occupations",
            "wikipedia_cause_of_death", "wikipedia_manner_of_death",
            "wikipedia_occupations",
        ]
        rows = [
            {
                "name": "Populated", "wikipedia_url": "https://en.wikipedia.org/wiki/Populated",
                "wikidata_id": "Q1", "birth_date": "1970", "death_date": "1997",
                "cause_of_death": "Alpha; Zeta", "manner_of_death": "accident",
                "age_status": "possible", "minimum_lifespan_days": "9500",
                "maximum_lifespan_days": "10200", "possible_age_range": "26 to 28",
                "occupations": "singer; writer",
                "wikipedia_cause_of_death": "fallback cause",
                "wikipedia_manner_of_death": "fallback manner",
                "wikipedia_occupations": "fallback occupation",
            },
            {
                "name": "Blank", "wikipedia_url": "https://en.wikipedia.org/wiki/Blank",
                "wikidata_id": "Q2", "birth_date": "1971", "death_date": "1998",
                "cause_of_death": "", "manner_of_death": "", "age_status": "confirmed",
                "minimum_lifespan_days": "9862", "maximum_lifespan_days": "9862",
                "possible_age_range": "27 years", "occupations": "",
                "wikipedia_cause_of_death": "somevalue",
                "wikipedia_manner_of_death": "somevalue",
                "wikipedia_occupations": "activist",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            original_root = build_data.ROOT
            build_data.ROOT = root
            try:
                result = build_data.load("sample.csv", "occupations")
            finally:
                build_data.ROOT = original_root

        self.assertEqual(result[0]["c"], ["Alpha", "Zeta"])
        self.assertEqual(result[0]["m"], ["accident"])
        self.assertEqual(result[0]["o"], ["singer", "writer"])
        self.assertFalse(result[0]["wc"])
        self.assertFalse(result[0]["wm"])
        self.assertFalse(result[0]["wo"])
        self.assertEqual(result[1]["c"], ["unknown"])
        self.assertEqual(result[1]["m"], ["unknown"])
        self.assertEqual(result[1]["o"], ["activist"])
        self.assertTrue(result[1]["wc"])
        self.assertTrue(result[1]["wm"])
        self.assertTrue(result[1]["wo"])


if __name__ == "__main__":
    unittest.main()
