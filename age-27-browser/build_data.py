#!/usr/bin/env python3
"""Compile the two CSV files into the browser's local JavaScript payload."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(__file__).with_name("data.js")


def split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def effective_values(row: dict[str, str], wikidata_column: str, wikipedia_column: str) -> tuple[list[str], bool]:
    wikidata_values = split_values(row.get(wikidata_column, ""))
    if wikidata_values:
        return wikidata_values, False
    wikipedia_values = [
        "unknown" if value == "somevalue" else value
        for value in split_values(row.get(wikipedia_column, ""))
    ]
    return wikipedia_values, bool(wikipedia_values)


def load(relative_path: str, occupation_column: str) -> list[dict[str, object]]:
    with (ROOT / relative_path).open(encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            causes, wikipedia_cause = effective_values(
                row, "cause_of_death", "wikipedia_cause_of_death"
            )
            manners, wikipedia_manner = effective_values(
                row, "manner_of_death", "wikipedia_manner_of_death"
            )
            occupations, wikipedia_occupation = effective_values(
                row, occupation_column, "wikipedia_occupations"
            )
            rows.append(
                {
                    "n": row["name"],
                    "u": row["wikipedia_url"],
                    "q": row["wikidata_id"],
                    "b": row["birth_date"],
                    "d": row["death_date"],
                    "c": causes,
                    "m": manners,
                    "s": row["age_status"],
                    "lo": int(row["minimum_lifespan_days"]),
                    "hi": int(row["maximum_lifespan_days"]),
                    "r": row["possible_age_range"],
                    "o": occupations,
                    "wc": wikipedia_cause,
                    "wm": wikipedia_manner,
                    "wo": wikipedia_occupation,
                }
            )
        return rows


def build_payload() -> dict[str, list[dict[str, object]]]:
    return {
        "people": load("age-27-people/age_27_people.csv", "occupations"),
        "musicians": load(
            "age-27-musicians/age_27_musicians.csv", "leaf_occupations"
        ),
    }


def main() -> None:
    payload = build_payload()
    OUTPUT.write_text(
        "window.AGE27_DATA = "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT} with {sum(map(len, payload.values())):,} records")


if __name__ == "__main__":
    main()
