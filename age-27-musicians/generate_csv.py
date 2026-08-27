#!/usr/bin/env python3
"""Generate a structured-data-only CSV of musicians who may have died at 27."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wikidata_age27 import (
    DataValidationError,
    GREGORIAN,
    JULIAN,
    QueryTimeout,
    RawPerson,
    StructuredTime,
    WDQSClient as SharedWDQSClient,
    batched,
    calculate_age_range as shared_calculate_age_range,
    format_calendar_age,
    qid_from_uri,
    time_sort_key,
)
from wikidata_age27.core import days_in_month


USER_AGENT = "Age27Musicians/1.0 (https://github.com/dsteinbock/wikipedia-lede)"
ENWIKI = "https://en.wikipedia.org/"

MUSICIAN = "Q639669"
ALLOW_ROOTS = {MUSICIAN, "Q1294626", "Q822146", "Q1198887"}
SUPPLEMENTAL_ALLOW_ROOTS = ALLOW_ROOTS - {MUSICIAN}
BLOCK_ROOTS = {"Q16145150", "Q125350850", "Q10730252"}
GENERIC_DISC_JOCKEY = "Q130857"

CSV_COLUMNS = [
    "name",
    "wikipedia_url",
    "wikidata_id",
    "birth_date",
    "death_date",
    "cause_of_death",
    "manner_of_death",
    "age_status",
    "minimum_lifespan_days",
    "maximum_lifespan_days",
    "possible_age_range",
    "leaf_occupations",
]


class WDQSClient(SharedWDQSClient):
    """Shared client configured with the original project's identifying agent."""

    def __init__(self, cache_dir: Path, **kwargs) -> None:
        super().__init__(cache_dir, user_agent=USER_AGENT, **kwargs)


calculate_age_range = shared_calculate_age_range


def discovery_query(roots: Iterable[str], year_filter: str | None = None) -> str:
    root_values = " ".join(f"wd:{qid}" for qid in sorted(roots))
    extra_filter = f"\n  FILTER({year_filter})" if year_filter else ""
    return f"""
SELECT DISTINCT ?person WHERE {{
  VALUES ?allowedRoot {{ {root_values} }}
  ?person wdt:P31 wd:Q5 ;
          wdt:P106 ?occupation ;
          wdt:P569 ?birth ;
          wdt:P570 ?death .
  ?occupation wdt:P279* ?allowedRoot .
  ?article schema:about ?person ;
           schema:isPartOf <{ENWIKI}> .
  FILTER((YEAR(?death) - YEAR(?birth)) IN (27, 28)){extra_filter}
}}
"""


DEATH_YEAR_BANDS = [
    "YEAR(?death) < 1800",
    "YEAR(?death) >= 1800 && YEAR(?death) < 1900",
    "YEAR(?death) >= 1900 && YEAR(?death) < 1950",
    "YEAR(?death) >= 1950 && YEAR(?death) < 2000",
    "YEAR(?death) >= 2000",
]


def _ids_from_result(payload: Mapping) -> set[str]:
    return {
        qid_from_uri(binding["person"]["value"])
        for binding in payload["results"]["bindings"]
    }


def discover_candidate_ids(client: WDQSClient) -> list[str]:
    candidate_ids: set[str] = set()
    for root in sorted(ALLOW_ROOTS):
        try:
            root_ids = _ids_from_result(client.query(discovery_query({root})))
        except QueryTimeout:
            root_ids: set[str] = set()
            for year_filter in DEATH_YEAR_BANDS:
                root_ids.update(
                    _ids_from_result(client.query(discovery_query({root}, year_filter)))
                )
        candidate_ids.update(root_ids)
    return sorted(candidate_ids)


def detail_query(qids: Sequence[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?person ?personLabel ?article
       ?birthTime ?birthPrecision ?birthCalendar
       ?deathTime ?deathPrecision ?deathCalendar
       ?occupation ?cause ?causeLabel ?manner ?mannerLabel WHERE {{
  VALUES ?person {{ {values} }}
  ?article schema:about ?person ;
           schema:isPartOf <{ENWIKI}> .
  ?person p:P569 ?birthStatement ;
          p:P570 ?deathStatement ;
          p:P106 ?occupationStatement .
  ?birthStatement a wikibase:BestRank ; psv:P569 ?birthValue .
  ?birthValue wikibase:timeValue ?birthTime ;
              wikibase:timePrecision ?birthPrecision ;
              wikibase:timeCalendarModel ?birthCalendar .
  ?deathStatement a wikibase:BestRank ; psv:P570 ?deathValue .
  ?deathValue wikibase:timeValue ?deathTime ;
              wikibase:timePrecision ?deathPrecision ;
              wikibase:timeCalendarModel ?deathCalendar .
  ?occupationStatement a wikibase:BestRank ; ps:P106 ?occupation .
  OPTIONAL {{
    ?person p:P509 ?causeStatement .
    ?causeStatement a wikibase:BestRank ; ps:P509 ?cause .
    FILTER(STRSTARTS(STR(?cause), "http://www.wikidata.org/entity/Q"))
    OPTIONAL {{
      ?cause rdfs:label ?causeLabel .
      FILTER(LANG(?causeLabel) = "en")
    }}
  }}
  OPTIONAL {{
    ?person p:P1196 ?mannerStatement .
    ?mannerStatement a wikibase:BestRank ; ps:P1196 ?manner .
    FILTER(STRSTARTS(STR(?manner), "http://www.wikidata.org/entity/Q"))
    OPTIONAL {{
      ?manner rdfs:label ?mannerLabel .
      FILTER(LANG(?mannerLabel) = "en")
    }}
  }}
  OPTIONAL {{
    ?person rdfs:label ?personLabel .
    FILTER(LANG(?personLabel) = "en")
  }}
}}
"""


def _article_title(url: str) -> str:
    return unquote(url.rsplit("/wiki/", 1)[-1]).replace("_", " ")


def _item_label(binding: Mapping, value_key: str, label_key: str) -> str | None:
    value = binding.get(value_key, {}).get("value", "")
    entity_prefix = "http://www.wikidata.org/entity/Q"
    if not value.startswith(entity_prefix):
        return None
    return binding.get(label_key, {}).get("value") or qid_from_uri(value)


def fetch_people(client: WDQSClient, qids: Sequence[str]) -> dict[str, RawPerson]:
    people: dict[str, RawPerson] = {}
    for batch in batched(list(qids), 50):
        payload = client.query(detail_query(batch))
        for binding in payload["results"]["bindings"]:
            qid = qid_from_uri(binding["person"]["value"])
            article = binding["article"]["value"]
            person = people.setdefault(qid, RawPerson(qid=qid))
            person.wikipedia_url = article
            person.name = binding.get("personLabel", {}).get("value") or _article_title(article)
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
            person.occupations.add(qid_from_uri(binding["occupation"]["value"]))
            cause = _item_label(binding, "cause", "causeLabel")
            manner = _item_label(binding, "manner", "mannerLabel")
            if cause:
                person.causes_of_death.add(cause)
            if manner:
                person.manners_of_death.add(manner)

    missing = set(qids) - set(people)
    if missing:
        raise DataValidationError(
            "Candidate detail query returned no usable row for: " + ", ".join(sorted(missing))
        )
    return people


def ancestry_query(occupations: Sequence[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in occupations)
    return f"""
SELECT ?occupation ?occupationLabel ?ancestor WHERE {{
  VALUES ?occupation {{ {values} }}
  ?occupation wdt:P279* ?ancestor .
  OPTIONAL {{
    ?occupation rdfs:label ?occupationLabel .
    FILTER(LANG(?occupationLabel) = "en")
  }}
}}
"""


def fetch_occupation_taxonomy(
    client: WDQSClient, occupations: Iterable[str]
) -> tuple[dict[str, set[str]], dict[str, str]]:
    ancestry: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    occupation_list = sorted(set(occupations))
    for batch in batched(occupation_list, 50):
        payload = client.query(ancestry_query(batch))
        for binding in payload["results"]["bindings"]:
            occupation = qid_from_uri(binding["occupation"]["value"])
            ancestor = qid_from_uri(binding["ancestor"]["value"])
            ancestry.setdefault(occupation, set()).add(ancestor)
            label = binding.get("occupationLabel", {}).get("value")
            if label:
                labels[occupation] = label
    for occupation in occupation_list:
        ancestry.setdefault(occupation, {occupation}).add(occupation)
    return ancestry, labels


def qualifying_occupations(
    occupations: Iterable[str], ancestry: Mapping[str, set[str]]
) -> set[str]:
    qualifying: set[str] = set()
    for occupation in occupations:
        ancestors = ancestry.get(occupation, {occupation})
        if occupation == GENERIC_DISC_JOCKEY:
            continue
        if ancestors & BLOCK_ROOTS:
            continue
        if ancestors & ALLOW_ROOTS:
            qualifying.add(occupation)
    return qualifying


def leaf_occupations(occupations: set[str], ancestry: Mapping[str, set[str]]) -> set[str]:
    return {
        occupation
        for occupation in occupations
        if not any(
            other != occupation and occupation in ancestry.get(other, set())
            for other in occupations
        )
    }


def build_rows(
    people: Mapping[str, RawPerson],
    ancestry: Mapping[str, set[str]],
    occupation_labels: Mapping[str, str],
) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    death_sort_keys: dict[str, int] = {}
    for qid, person in people.items():
        qualifying = qualifying_occupations(person.occupations, ancestry)
        leaves = leaf_occupations(qualifying, ancestry)
        if not leaves:
            continue
        try:
            age_range = calculate_age_range(person.births, person.deaths)
        except DataValidationError:
            continue
        if age_range is None:
            continue

        birth_display = "; ".join(
            value.display() for value in sorted(person.births, key=time_sort_key)
        )
        death_display = "; ".join(
            value.display() for value in sorted(person.deaths, key=time_sort_key)
        )
        leaf_display = "; ".join(
            sorted((occupation_labels.get(value, value) for value in leaves), key=str.casefold)
        )
        rows.append(
            {
                "name": person.name,
                "wikipedia_url": person.wikipedia_url,
                "wikidata_id": qid,
                "birth_date": birth_display,
                "death_date": death_display,
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
                "leaf_occupations": leaf_display,
            }
        )
        death_sort_keys[qid] = min(
            value.bounds()[0].to_jdn() for value in person.deaths
        )

    rows.sort(
        key=lambda row: (
            death_sort_keys[str(row["wikidata_id"])],
            str(row["name"]).casefold(),
            str(row["wikidata_id"]),
        )
    )
    return rows


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
        if not row["leaf_occupations"]:
            raise DataValidationError(f"No qualifying leaf occupation for {qid}")
        if row["age_status"] not in {"confirmed", "possible"}:
            raise DataValidationError(f"Invalid age status for {qid}")
        if int(row["minimum_lifespan_days"]) > int(row["maximum_lifespan_days"]):
            raise DataValidationError(f"Reversed lifespan bounds for {qid}")


def write_csv(rows: Sequence[Mapping[str, object]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def generate(output: Path, cache_dir: Path) -> list[dict[str, str | int]]:
    client = WDQSClient(cache_dir)
    candidate_ids = discover_candidate_ids(client)
    people = fetch_people(client, candidate_ids)
    occupations = {occupation for person in people.values() for occupation in person.occupations}
    ancestry, occupation_labels = fetch_occupation_taxonomy(client, occupations)
    rows = build_rows(people, ancestry, occupation_labels)
    validate_rows(rows)
    write_csv(rows, output)
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=project_dir / "age_27_musicians.csv",
        help="CSV destination (default: age_27_musicians.csv beside this script)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=project_dir / ".cache",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    rows = generate(args.output, args.cache_dir)
    confirmed = sum(row["age_status"] == "confirmed" for row in rows)
    possible = sum(row["age_status"] == "possible" for row in rows)
    print(f"Wrote {len(rows)} rows to {args.output}")
    print(f"Confirmed: {confirmed}; possible: {possible}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
