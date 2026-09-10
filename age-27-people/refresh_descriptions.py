#!/usr/bin/env python3
"""Fill the existing public cohort's English Wikidata descriptions in place."""

from __future__ import annotations

import argparse
import csv
import re
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wikidata_age27 import DataValidationError, GraphQLClient, batched

from generate_csv import CSV_COLUMNS, USER_AGENT


QID_RE = re.compile(r"Q[1-9][0-9]*$")
YEAR_RE = re.compile(r"(?<!\d)(-?\d{1,6})(?!\d)")
PUNCTUATION_RE = re.compile(r"[\s.,;:]+$")
PARENTHETICAL_RE = re.compile(r"\s*\((?P<value>[^()]*)\)$")
LIFESPAN_VALUE_RE = re.compile(r"[\s*†c./,\d–—-]+$", re.IGNORECASE)
EXPLICIT_DEATH_RE = re.compile(
    r"(?P<before>.*?)(?:\s*\(\s*|[\s,;]+)(?:died|d\.)\s*(?P<year>-?\d{1,6})\s*\)?$",
    re.IGNORECASE,
)


def description_query(qids: Sequence[str]) -> str:
    ids = ", ".join(f'"{qid}"' for qid in qids)
    return f"""
query age27Descriptions {{
  itemsById(ids: [{ids}]) {{
    id
    description(languageCode: "en")
  }}
}}
"""


def fetch_descriptions(client: GraphQLClient, qids: Iterable[str]) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for batch in batched(sorted(qids), 50):
        payload = client.query(description_query(batch))
        items = payload.get("data", {}).get("itemsById") or []
        returned: set[str] = set()
        for item in items:
            if not item:
                continue
            qid = str(item.get("id", ""))
            if qid not in batch:
                raise DataValidationError(f"Unexpected GraphQL item: {qid}")
            returned.add(qid)
            descriptions[qid] = str(item.get("description") or "")
        missing = set(batch) - returned
        if missing:
            raise DataValidationError(
                "GraphQL returned no item for: " + ", ".join(sorted(missing))
            )
    return descriptions


def _years(value: str) -> set[str]:
    return set(YEAR_RE.findall(value))


def normalize_description(description: str, birth_date: str, death_date: str) -> str:
    """Remove only trailing date boilerplate that identifies this person's lifespan."""

    cleaned = PUNCTUATION_RE.sub("", description.strip())
    birth_years = _years(birth_date)
    death_years = _years(death_date)
    if not cleaned or not death_years:
        return cleaned

    parenthetical = PARENTHETICAL_RE.search(cleaned)
    if parenthetical:
        value = parenthetical.group("value")
        mentioned = _years(value)
        if (
            LIFESPAN_VALUE_RE.fullmatch(value)
            and birth_years & mentioned
            and death_years & mentioned
        ):
            cleaned = cleaned[:parenthetical.start()]

    direct_lifespan = re.search(r"\s+(?P<value>[*†\s\d–—-]+)$", cleaned)
    if direct_lifespan:
        value = direct_lifespan.group("value")
        mentioned = _years(value)
        if birth_years & mentioned and death_years & mentioned:
            cleaned = cleaned[:direct_lifespan.start()]

    explicit_death = EXPLICIT_DEATH_RE.match(cleaned)
    if explicit_death and explicit_death.group("year") in death_years:
        cleaned = explicit_death.group("before")

    return PUNCTUATION_RE.sub("", cleaned)


def normalize_rows(rows: Sequence[dict[str, str]]) -> int:
    changed = 0
    for row in rows:
        normalized = normalize_description(
            row.get("description", ""), row.get("birth_date", ""), row.get("death_date", "")
        )
        if normalized != row.get("description", ""):
            row["description"] = normalized
            changed += 1
    return changed


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise DataValidationError("CSV is missing a header row")
        expected_without_descriptions = [column for column in CSV_COLUMNS if column != "description"]
        if reader.fieldnames not in (CSV_COLUMNS, expected_without_descriptions):
            raise DataValidationError("CSV schema does not match the public cohort schema")
        rows = list(reader)
    qids = [str(row.get("wikidata_id", "")) for row in rows]
    if len(set(qids)) != len(qids) or any(not QID_RE.fullmatch(qid) for qid in qids):
        raise DataValidationError("CSV has missing, invalid, or duplicate Wikidata IDs")
    return rows


def write_rows(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    temporary.replace(path)


def refresh(input_path: Path, cache_dir: Path, normalize_existing: bool = False) -> tuple[int, int]:
    rows = read_rows(input_path)
    if not normalize_existing:
        descriptions = fetch_descriptions(
            GraphQLClient(cache_dir / "descriptions", user_agent=USER_AGENT),
            (row["wikidata_id"] for row in rows),
        )
        for row in rows:
            row["description"] = descriptions[row["wikidata_id"]]
    changed = normalize_rows(rows)
    write_rows(input_path, rows)
    return len(rows), changed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=project_dir / "age_27_people.csv")
    parser.add_argument("--cache-dir", type=Path, default=project_dir / ".cache", help=argparse.SUPPRESS)
    parser.add_argument(
        "--normalize-existing",
        action="store_true",
        help="normalize the descriptions already in the CSV without querying Wikidata",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    count, changed = refresh(args.input, args.cache_dir, args.normalize_existing)
    print(f"Wrote English Wikidata descriptions for {count:,} people to {args.input} ({changed:,} normalized)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
