#!/usr/bin/env python3
"""Generate all English-Wikipedia people who definitely or possibly died at 27."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wikidata_age27 import (
    CSV_BASE_COLUMNS,
    DataValidationError,
    GraphQLClient,
    QueryTimeout,
    RawPerson,
    StructuredTime,
    WDQSClient,
    batched,
    calculate_age_range,
    qid_from_uri,
    time_sort_key,
)


ENWIKI = "https://en.wikipedia.org/"
USER_AGENT = "Age27People/1.0 (https://github.com/dsteinbock/wikipedia-lede)"
WIKIPEDIA_FALLBACK_COLUMNS = [
    "wikipedia_cause_of_death",
    "wikipedia_cause_of_death_qids",
    "wikipedia_manner_of_death",
    "wikipedia_manner_of_death_qids",
    "wikipedia_occupations",
    "wikipedia_occupation_qids",
    "wikipedia_death_review_status",
]
CSV_COLUMNS = CSV_BASE_COLUMNS + ["occupations"] + WIKIPEDIA_FALLBACK_COLUMNS
REMOVED_ENTRY_METADATA_COLUMNS = ["removal_reason", "removed_utc", "source_run_dir"]
REMOVED_ENTRY_COLUMNS = CSV_COLUMNS + REMOVED_ENTRY_METADATA_COLUMNS
DEATH_REVIEW_STATUSES = {
    "",
    "settled",
    "provisional",
    "disputed",
    "unknown",
    "possible_removal",
}
INITIAL_SHARDS = [(None, 1800), (1800, 1900), (1900, 1950), (1950, 2000)]


@dataclass(frozen=True)
class CrawlStats:
    candidate_count: int
    included_count: int
    confirmed_count: int
    possible_count: int
    exclusions: Mapping[str, int]


def _xsd_year(year: int) -> str:
    if year >= 0:
        return f"{year:04d}"
    return f"-{abs(year):04d}"


def shard_query(start_year: int | None, end_year: int) -> str:
    bounds = []
    if start_year is not None:
        bounds.append(
            f'FILTER(?candidateDeath >= "{_xsd_year(start_year)}-01-01T00:00:00Z"^^xsd:dateTime)'
        )
    bounds.append(
        f'FILTER(?candidateDeath < "{_xsd_year(end_year)}-01-01T00:00:00Z"^^xsd:dateTime)'
    )
    bound_text = "\n    ".join(bounds)
    return f"""
SELECT ?person
       ?birthTime ?birthPrecision ?birthCalendar
       ?deathTime ?deathPrecision ?deathCalendar
WITH {{
  SELECT DISTINCT ?person WHERE {{
    ?person wdt:P31 wd:Q5 ;
            wdt:P569 ?candidateBirth ;
            wdt:P570 ?candidateDeath . hint:Prior hint:rangeSafe true .
    ?article schema:about ?person ;
             schema:isPartOf <{ENWIKI}> .
    {bound_text}
    FILTER((YEAR(?candidateDeath) - YEAR(?candidateBirth)) IN (27, 28))
  }}
}} AS %candidatePeople
WHERE {{
  INCLUDE %candidatePeople .
  ?person p:P569 ?birthStatement ;
          p:P570 ?deathStatement .
  ?birthStatement a wikibase:BestRank ; psv:P569 ?birthValue .
  ?birthValue wikibase:timeValue ?birthTime ;
              wikibase:timePrecision ?birthPrecision ;
              wikibase:timeCalendarModel ?birthCalendar .
  ?deathStatement a wikibase:BestRank ; psv:P570 ?deathValue .
  ?deathValue wikibase:timeValue ?deathTime ;
              wikibase:timePrecision ?deathPrecision ;
              wikibase:timeCalendarModel ?deathCalendar .
}}
"""


def _parse_shard(payload: Mapping) -> dict[str, RawPerson]:
    people: dict[str, RawPerson] = {}
    for binding in payload["results"]["bindings"]:
        qid = qid_from_uri(binding["person"]["value"])
        person = people.setdefault(qid, RawPerson(qid=qid))
        person.births.add(
            StructuredTime(
                binding["birthTime"]["value"],
                int(binding["birthPrecision"]["value"]),
                qid_from_uri(binding["birthCalendar"]["value"]),
            )
        )
        person.deaths.add(
            StructuredTime(
                binding["deathTime"]["value"],
                int(binding["deathPrecision"]["value"]),
                qid_from_uri(binding["deathCalendar"]["value"]),
            )
        )
    return people


def _split_shard(start_year: int | None, end_year: int) -> tuple[tuple[int | None, int], tuple[int, int]]:
    if start_year is None:
        if end_year > 1000:
            midpoint = 1000
        elif end_year > 0:
            midpoint = 0
        else:
            midpoint = end_year - 1000
    else:
        if end_year - start_year <= 1:
            raise QueryTimeout(
                f"WDQS could not complete the irreducible {start_year} death-year shard"
            )
        midpoint = start_year + (end_year - start_year) // 2
    return (start_year, midpoint), (midpoint, end_year)


def _merge_people(destination: dict[str, RawPerson], source: Mapping[str, RawPerson]) -> None:
    for qid, incoming in source.items():
        person = destination.setdefault(qid, RawPerson(qid=qid))
        person.births.update(incoming.births)
        person.deaths.update(incoming.deaths)


def fetch_shard(
    client: WDQSClient, start_year: int | None, end_year: int
) -> dict[str, RawPerson]:
    try:
        return _parse_shard(client.query(shard_query(start_year, end_year)))
    except QueryTimeout:
        left, right = _split_shard(start_year, end_year)
        people = fetch_shard(client, *left)
        _merge_people(people, fetch_shard(client, *right))
        return people


def discover_people(client: WDQSClient, current_year: int | None = None) -> dict[str, RawPerson]:
    last_year = (current_year or datetime.now(timezone.utc).year) + 1
    people: dict[str, RawPerson] = {}
    for start, end in [*INITIAL_SHARDS, (2000, last_year)]:
        _merge_people(people, fetch_shard(client, start, end))
    return people


def enrichment_query(qids: Sequence[str]) -> str:
    ids = ", ".join(f'"{qid}"' for qid in qids)
    return f"""
query age27People {{
  itemsById(ids: [{ids}]) {{
    id
    label(languageCode: "en")
    description(languageCode: "en")
    sitelink(siteId: "enwiki") {{ title url }}
    occupations: statements(propertyId: "P106") {{
      rank
      value {{
        ... on ItemValue {{ id label(languageCode: "en") }}
      }}
    }}
    causesOfDeath: statements(propertyId: "P509") {{
      rank
      value {{
        ... on ItemValue {{ id label(languageCode: "en") }}
      }}
    }}
    mannersOfDeath: statements(propertyId: "P1196") {{
      rank
      value {{
        ... on ItemValue {{ id label(languageCode: "en") }}
      }}
    }}
  }}
}}
"""


def _best_occupation_labels(statements: Iterable[Mapping]) -> tuple[set[str], set[str]]:
    usable = [statement for statement in statements if statement.get("rank") != "DEPRECATED"]
    preferred = [statement for statement in usable if statement.get("rank") == "PREFERRED"]
    chosen = preferred or [statement for statement in usable if statement.get("rank") == "NORMAL"]
    ids: set[str] = set()
    labels: set[str] = set()
    for statement in chosen:
        value = statement.get("value") or {}
        qid = value.get("id")
        if qid:
            ids.add(qid)
            labels.add(value.get("label") or qid)
    return ids, labels


def enrich_people(client: GraphQLClient, people: Mapping[str, RawPerson]) -> dict[str, set[str]]:
    occupation_labels: dict[str, set[str]] = {}
    for batch in batched(sorted(people), 50):
        payload = client.query(enrichment_query(batch))
        items = payload.get("data", {}).get("itemsById") or []
        returned: set[str] = set()
        for item in items:
            if not item:
                continue
            qid = item["id"]
            if qid not in people:
                raise DataValidationError(f"Unexpected GraphQL item: {qid}")
            returned.add(qid)
            sitelink = item.get("sitelink") or {}
            if not sitelink.get("url"):
                raise DataValidationError(f"Missing English Wikipedia sitelink for {qid}")
            person = people[qid]
            person.name = item.get("label") or sitelink.get("title") or qid
            person.description = item.get("description") or ""
            person.wikipedia_url = sitelink["url"]
            occupation_ids, labels = _best_occupation_labels(item.get("occupations") or [])
            person.occupations = occupation_ids
            occupation_labels[qid] = labels
            _, person.causes_of_death = _best_occupation_labels(
                item.get("causesOfDeath") or []
            )
            _, person.manners_of_death = _best_occupation_labels(
                item.get("mannersOfDeath") or []
            )
        missing = set(batch) - returned
        if missing:
            raise DataValidationError(
                "GraphQL returned no item for: " + ", ".join(sorted(missing))
            )
    return occupation_labels


def build_rows(
    people: Mapping[str, RawPerson], occupation_labels: Mapping[str, set[str]]
) -> tuple[list[dict[str, str | int]], Counter[str]]:
    rows: list[dict[str, str | int]] = []
    exclusions: Counter[str] = Counter()
    death_sort_keys: dict[str, int] = {}
    for qid, person in people.items():
        try:
            age_range = calculate_age_range(person.births, person.deaths)
        except DataValidationError as exc:
            reason = "unsupported_or_invalid_date"
            if "chronology" in str(exc).lower() or "precedes" in str(exc).lower():
                reason = "invalid_chronology"
            exclusions[reason] += 1
            continue
        if age_range is None:
            exclusions["outside_age_rule"] += 1
            continue
        rows.append(
            {
                "name": person.name,
                "description": person.description,
                "wikipedia_url": person.wikipedia_url,
                "wikidata_id": qid,
                "birth_date": "; ".join(
                    value.display() for value in sorted(person.births, key=time_sort_key)
                ),
                "death_date": "; ".join(
                    value.display() for value in sorted(person.deaths, key=time_sort_key)
                ),
                "cause_of_death": "; ".join(
                    sorted(person.causes_of_death, key=str.casefold)
                ),
                "manner_of_death": "; ".join(
                    sorted(person.manners_of_death, key=str.casefold)
                ),
                "age_status": age_range.status,
                "minimum_lifespan_days": age_range.minimum_days,
                "maximum_lifespan_days": age_range.maximum_days,
                "possible_age_range": age_range.display(),
                "occupations": "; ".join(
                    sorted(occupation_labels.get(qid, set()), key=str.casefold)
                ),
                **{column: "" for column in WIKIPEDIA_FALLBACK_COLUMNS},
            }
        )
        death_sort_keys[qid] = min(value.bounds()[0].to_jdn() for value in person.deaths)
    rows.sort(
        key=lambda row: (
            death_sort_keys[str(row["wikidata_id"])],
            str(row["name"]).casefold(),
            str(row["wikidata_id"]),
        )
    )
    return rows, exclusions


def load_wikipedia_fallbacks(output: Path) -> dict[str, dict[str, str]]:
    """Load hand-reviewed Wikipedia fields so a Wikidata refresh cannot erase them."""
    if not output.exists():
        return {}
    with output.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "wikidata_id" not in reader.fieldnames:
            raise DataValidationError("Existing CSV has no wikidata_id column")
        return {
            row["wikidata_id"]: {
                column: row.get(column, "") for column in WIKIPEDIA_FALLBACK_COLUMNS
            }
            for row in reader
            if row.get("wikidata_id")
        }


def load_removed_qids(path: Path) -> set[str]:
    """Load the permanent exclusion ledger used by future crawls."""

    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != REMOVED_ENTRY_COLUMNS:
            raise DataValidationError("Removed-entry ledger schema mismatch")
        qids: set[str] = set()
        for row in reader:
            qid = str(row.get("wikidata_id", "")).strip()
            if not re.fullmatch(r"Q[1-9][0-9]*", qid):
                raise DataValidationError(f"Invalid removed-entry QID: {qid}")
            if qid in qids:
                raise DataValidationError(f"Duplicate removed-entry QID: {qid}")
            if not str(row.get("removal_reason", "")).strip():
                raise DataValidationError(f"Removed entry has no reason: {qid}")
            if not str(row.get("removed_utc", "")).strip():
                raise DataValidationError(f"Removed entry has no timestamp: {qid}")
            if not str(row.get("source_run_dir", "")).strip():
                raise DataValidationError(f"Removed entry has no source run: {qid}")
            qids.add(qid)
        return qids


def exclude_removed_rows(
    rows: Sequence[dict[str, str | int]], removed_qids: set[str]
) -> tuple[list[dict[str, str | int]], int]:
    kept = [row for row in rows if str(row["wikidata_id"]) not in removed_qids]
    return kept, len(rows) - len(kept)


def apply_wikipedia_fallbacks(
    rows: Sequence[dict[str, str | int]],
    fallbacks: Mapping[str, Mapping[str, str]],
) -> None:
    for row in rows:
        preserved = fallbacks.get(str(row["wikidata_id"]), {})
        for column in WIKIPEDIA_FALLBACK_COLUMNS:
            row[column] = preserved.get(column, "")


def _split_field(value: object) -> list[str]:
    return [item.strip() for item in str(value).split(";") if item.strip()]


def _validate_wikipedia_pairs(row: Mapping[str, object], label_column: str, qid_column: str) -> None:
    qid = str(row["wikidata_id"])
    labels = _split_field(row[label_column])
    qids = _split_field(row[qid_column])
    if labels != sorted(labels, key=str.casefold):
        raise DataValidationError(f"Unsorted Wikipedia fallback values for {qid}")
    if labels == ["somevalue"] and not qids:
        return
    if len(labels) != len(qids):
        raise DataValidationError(f"Unaligned Wikipedia fallback labels/QIDs for {qid}")
    if any(not re.fullmatch(r"Q[1-9][0-9]*", item) for item in qids):
        raise DataValidationError(f"Invalid Wikipedia fallback QID for {qid}")


def validate_rows(rows: Sequence[Mapping[str, object]]) -> None:
    seen: set[str] = set()
    for row in rows:
        if list(row.keys()) != CSV_COLUMNS:
            raise DataValidationError("CSV row columns do not match the public schema")
        qid = str(row["wikidata_id"])
        if qid in seen:
            raise DataValidationError(f"Duplicate Wikidata ID: {qid}")
        seen.add(qid)
        if not str(row["wikipedia_url"]).startswith("https://en.wikipedia.org/wiki/"):
            raise DataValidationError(f"Non-English-Wikipedia URL for {qid}")
        if row["age_status"] not in {"confirmed", "possible"}:
            raise DataValidationError(f"Invalid age status for {qid}")
        if int(row["minimum_lifespan_days"]) > int(row["maximum_lifespan_days"]):
            raise DataValidationError(f"Reversed lifespan bounds for {qid}")
        _validate_wikipedia_pairs(
            row, "wikipedia_cause_of_death", "wikipedia_cause_of_death_qids"
        )
        _validate_wikipedia_pairs(
            row, "wikipedia_manner_of_death", "wikipedia_manner_of_death_qids"
        )
        _validate_wikipedia_pairs(
            row, "wikipedia_occupations", "wikipedia_occupation_qids"
        )
        if row["wikipedia_death_review_status"] not in DEATH_REVIEW_STATUSES:
            raise DataValidationError(f"Invalid Wikipedia death review status for {qid}")


def write_csv(rows: Sequence[Mapping[str, object]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def generate(
    output: Path,
    cache_dir: Path,
    removed_entries: Path | None = None,
) -> tuple[list[dict[str, str | int]], CrawlStats]:
    fallbacks = load_wikipedia_fallbacks(output)
    removed_entries = removed_entries or output.parent / "removed_entries.csv"
    removed_qids = load_removed_qids(removed_entries)
    wdqs = WDQSClient(cache_dir / "wdqs", user_agent=USER_AGENT)
    people = discover_people(wdqs)
    candidate_count = len(people)
    if removed_qids:
        # Do not spend GraphQL enrichment requests on permanently excluded QIDs.
        people = {qid: person for qid, person in people.items() if qid not in removed_qids}
    graphql = GraphQLClient(cache_dir / "graphql", user_agent=USER_AGENT)
    labels = enrich_people(graphql, people)
    rows, exclusions = build_rows(people, labels)
    rows, removed_count = exclude_removed_rows(rows, removed_qids)
    if removed_count:
        exclusions["removed_entries"] += removed_count
    apply_wikipedia_fallbacks(rows, fallbacks)
    validate_rows(rows)
    write_csv(rows, output)
    stats = CrawlStats(
        candidate_count=candidate_count,
        included_count=len(rows),
        confirmed_count=sum(row["age_status"] == "confirmed" for row in rows),
        possible_count=sum(row["age_status"] == "possible" for row in rows),
        exclusions=dict(sorted(exclusions.items())),
    )
    return rows, stats


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=project_dir / "age_27_people.csv")
    parser.add_argument(
        "--removed-entries", type=Path, default=project_dir / "removed_entries.csv"
    )
    parser.add_argument("--cache-dir", type=Path, default=project_dir / ".cache", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _, stats = generate(args.output, args.cache_dir, args.removed_entries)
    print(f"Candidates discovered: {stats.candidate_count}")
    print(f"Wrote {stats.included_count} rows to {args.output}")
    print(f"Confirmed: {stats.confirmed_count}; possible: {stats.possible_count}")
    for reason, count in stats.exclusions.items():
        print(f"Excluded ({reason}): {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
