#!/usr/bin/env python3
"""Deterministic mechanics for Wikipedia fallback enrichment batches.

The helper deliberately does not infer semantic facts.  It freezes cohorts,
retrieves/cache-validates article source, prepares complete semantic packets,
validates LLM-authored proposals, applies them atomically, and checks invariants.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wikidata_age27.core import GREGORIAN, StructuredTime, calendar_age, format_calendar_age


PEOPLE_CSV = PROJECT_DIR / "age_27_people.csv"
MUSICIANS_CSV = REPO_ROOT / "age-27-musicians" / "age_27_musicians.csv"
CLUB_ARCHIVE_HTML = REPO_ROOT / "age-27-musicians" / "purported-27-club-members.html"
BROWSER_BUILDER = REPO_ROOT / "age-27-browser" / "build_data.py"
BROWSER_DATA = REPO_ROOT / "age-27-browser" / "data.js"
CACHE_ROOT = PROJECT_DIR / ".cache" / "wikipedia-fallback"
REVIEW_CSV = PROJECT_DIR / "wikipedia_stronger_model_review.csv"
AMBIGUOUS_REVIEW_CSV = PROJECT_DIR / "wikipedia_ambiguous_members_review.csv"
REMOVED_ENTRIES_CSV = PROJECT_DIR / "removed_entries.csv"
TRUSTED_VOCABULARY = PROJECT_DIR / "wikipedia_fallback_vocabulary.json"
APPROVED_VOCABULARY = CACHE_ROOT / "approved-vocabulary.json"
USER_AGENT = (
    "Age27PeopleWikipediaFallback/2.1 "
    "(https://github.com/dsteinbock/wikipedia-lede; "
    "https://github.com/dsteinbock/wikipedia-lede/issues)"
)

FALLBACK_COLUMNS = [
    "wikipedia_cause_of_death",
    "wikipedia_cause_of_death_qids",
    "wikipedia_manner_of_death",
    "wikipedia_manner_of_death_qids",
    "wikipedia_occupations",
    "wikipedia_occupation_qids",
    "wikipedia_death_review_status",
]
FIELD_SPECS = {
    "cause": (
        "cause_of_death",
        "wikipedia_cause_of_death",
        "wikipedia_cause_of_death_qids",
    ),
    "manner": (
        "manner_of_death",
        "wikipedia_manner_of_death",
        "wikipedia_manner_of_death_qids",
    ),
    "occupation": (
        "occupations",
        "wikipedia_occupations",
        "wikipedia_occupation_qids",
    ),
}
REVIEW_COLUMNS = [
    "wikidata_id",
    "name",
    "article_url",
    "language",
    "revision_id",
    "article_bytes",
    "status",
    "proposed_cause",
    "proposed_cause_qid",
    "proposed_manner",
    "proposed_manner_qid",
    "proposed_occupation",
    "proposed_occupation_qid",
    "evidence_basis",
]
AMBIGUOUS_REVIEW_COLUMNS = [
    "wikidata_id",
    "name",
    "article_url",
    "revision_id",
    "member_classes",
    "existing_age_status",
    "wikidata_birth_dates",
    "wikidata_death_dates",
    "source_row_sha256",
    "confirmed_age_at_death",
    "recommended_birth_date",
    "recommended_death_date",
    "evidence_sources",
    "evidence_excerpts",
    "conflict",
    "recommended_action",
    "reason",
    "reviewed_utc",
    "source_run_dir",
]
REMOVED_ENTRY_METADATA_COLUMNS = ["removal_reason", "removed_utc", "source_run_dir"]
STATUSES = {"settled", "provisional", "disputed", "unknown", "possible_removal"}
REMOVAL_REASONS = {
    "living",
    "nonhuman",
    "age_outside_26_28",
    "no_dedicated_person_article",
    "subject_identity_mismatch",
}
STAY_OVERRIDES = {
    "approved_musician_occupation",
    "archived_27_club_article",
}
PAGE_KINDS = {"person", "event", "case", "list", "group", "other"}
HUMAN_STATUSES = {"human", "nonhuman", "unclear"}
LIFE_STATUSES = {"deceased", "living", "conflicting", "unclear"}
AGE_COMPATIBILITIES = {
    "compatible",
    "outside_26_28",
    "conflicting",
    "unknown",
}
SUBJECT_MATCHES = {"match", "mismatch", "unclear"}
SPECIAL_VALUES = {"somevalue", "novalue"}
QID_RE = re.compile(r"Q[1-9][0-9]*")
DATE_RE = re.compile(r"^([+-]?\d+)(?:-(\d{2}))?(?:-(\d{2}))?$")
AMBIGUOUS_ACTIONS = {"discard", "elevate", "update", "review", "none"}
AMBIGUOUS_REVIEW_POLICY_VERSION = 3
AMBIGUOUS_VERDICTS = {"confirmed", "rejected", "ambiguous"}
AMBIGUOUS_CLAIM_TYPES = {"age_at_death", "birth_date", "death_date", "other"}
AMBIGUOUS_ASSIGNMENT_LIMITS = {"max_bytes": 64 * 1024, "max_items": 20}
MONTH_NUMBERS = {
    name: number
    for number, names in enumerate(
        (
            (),
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        )
    )
    for name in names
}
SOURCE_TIERS = {
    "lead_sentence",
    "rest_of_lead_paragraph",
    "remaining_lead_section",
    "infobox",
    "rest_of_article",
    "none",
}
DEATH_REVIEW_TIERS = [
    "lead_sentence",
    "rest_of_lead_paragraph",
    "remaining_lead_section",
    "infobox",
    "rest_of_article",
]
# Retained only so pre-v2 proposal files fail cleanly during legacy validation.
# The semantic pipeline does not generate keyword candidates or unknown audits.
UNKNOWN_DISPOSITIONS = {
    "not_about_subject",
    "does_not_establish_field",
    "explicitly_unknown",
    "unconfirmed_without_usable_account",
}
HTTP_STATS = {"requests": 0, "retries": 0, "failed_requests": 0}
TRANSIENT_API_ERRORS = {
    "internal_api_error_DBConnectionError",
    "internal_api_error_DBQueryError",
    "maxlag",
    "ratelimited",
    "readonly",
}


class BatchError(RuntimeError):
    """Raised when deterministic batch invariants fail."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise BatchError(f"Missing CSV header: {path}")
        return list(reader.fieldnames), list(reader)


def removed_entry_columns(public_fields: Sequence[str]) -> list[str]:
    return list(public_fields) + REMOVED_ENTRY_METADATA_COLUMNS


def load_removed_entries(
    path: Path, public_fields: Sequence[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Read and validate the permanent exclusion ledger, if present."""

    expected = removed_entry_columns(public_fields)
    if not path.exists():
        return expected, []
    fields, rows = read_csv(path)
    if fields != expected:
        raise BatchError(f"Removed-entry ledger schema mismatch: {path}")
    validate_removed_entry_rows(rows)
    return fields, rows


def validate_removed_entry_rows(rows: Sequence[Mapping[str, str]]) -> None:
    """Validate ledger identity and audit metadata without touching the filesystem."""

    seen: set[str] = set()
    for row in rows:
        qid = row.get("wikidata_id", "").strip()
        if not QID_RE.fullmatch(qid):
            raise BatchError(f"Invalid removed-entry QID: {qid!r}")
        if qid in seen:
            raise BatchError(f"Duplicate removed-entry QID: {qid}")
        seen.add(qid)
        if not row.get("removal_reason", "").strip():
            raise BatchError(f"Removed entry has no reason: {qid}")
        if not row.get("removed_utc", "").strip():
            raise BatchError(f"Removed entry has no timestamp: {qid}")
        if not row.get("source_run_dir", "").strip():
            raise BatchError(f"Removed entry has no source run: {qid}")


def _normalize_enwiki_article_url(value: object) -> str:
    """Return a canonical URL key for a final English Wikipedia article."""

    raw = html.unescape(str(value or "").strip())
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.casefold() != "https" or parsed.netloc.casefold() != "en.wikipedia.org":
        return ""
    path = urllib.parse.unquote(parsed.path).rstrip("/")
    if not path.startswith("/wiki/") or len(path) <= len("/wiki/"):
        return ""
    return f"https://en.wikipedia.org{path}"


def _load_approved_musician_qids() -> set[str]:
    """Load the materialized people selected by the approved musician hierarchy."""

    if not MUSICIANS_CSV.exists():
        raise BatchError(f"Approved musician dataset is missing: {MUSICIANS_CSV}")
    fields, rows = read_csv(MUSICIANS_CSV)
    if "wikidata_id" not in fields:
        raise BatchError(f"Approved musician dataset lacks wikidata_id: {MUSICIANS_CSV}")
    qids = {row["wikidata_id"].strip() for row in rows}
    return {qid for qid in qids if QID_RE.fullmatch(qid)}


def _load_archived_27_club_urls() -> set[str]:
    """Read only first-cell person links from the frozen 27 Club HTML table."""

    if not CLUB_ARCHIVE_HTML.exists():
        raise BatchError(f"27 Club HTML archive is missing: {CLUB_ARCHIVE_HTML}")
    source = html.unescape(CLUB_ARCHIVE_HTML.read_text(encoding="utf-8"))
    urls: set[str] = set()
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, flags=re.I | re.S):
        first_cell = re.search(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
        if not first_cell:
            continue
        match = re.search(
            r"href=[\"'](https://en\.wikipedia\.org/wiki/[^\"']+)",
            first_cell.group(1),
            flags=re.I,
        )
        if match:
            normalized = _normalize_enwiki_article_url(match.group(1))
            if normalized:
                urls.add(normalized)
    return urls


def split_values(value: object) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def usable_wikidata(value: object) -> bool:
    return any(part.casefold() not in SPECIAL_VALUES for part in split_values(value))


def field_is_effective(row: Mapping[str, str], field: str) -> bool:
    base, fallback, _ = FIELD_SPECS[field]
    return usable_wikidata(row.get(base, "")) or bool(str(row.get(fallback, "")).strip())


def effective_labels(row: Mapping[str, str], field: str) -> list[str]:
    base, fallback, _ = FIELD_SPECS[field]
    if usable_wikidata(row.get(base, "")):
        return split_values(row.get(base, ""))
    return split_values(row.get(fallback, ""))


def row_is_terminal(row: Mapping[str, str]) -> bool:
    return str(row.get("wikipedia_death_review_status", "")).strip() == (
        "possible_removal"
    ) or all(field_is_effective(row, field) for field in FIELD_SPECS)


def load_target_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("people"), list):
        raise BatchError(f"Invalid target manifest: {path}")
    qids = [str(item.get("wikidata_id", "")) for item in value["people"]]
    if not qids or len(qids) != len(set(qids)) or any(
        not QID_RE.fullmatch(qid) for qid in qids
    ):
        raise BatchError(f"Invalid target QIDs in manifest: {path}")
    if int(value.get("expected_count", -1)) != len(qids):
        raise BatchError(f"Target manifest count mismatch: {path}")
    return value


def parse_latest_possible_date(value: str) -> tuple[int, int, int]:
    alternatives: list[tuple[int, int, int]] = []
    for raw in value.split(";"):
        text = raw.strip()
        match = DATE_RE.fullmatch(text)
        if not match:
            raise BatchError(f"Invalid death-date alternative: {text!r}")
        year = int(match.group(1))
        month = int(match.group(2)) if match.group(2) else 12
        day = int(match.group(3)) if match.group(3) else 31
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            raise BatchError(f"Invalid death-date alternative: {text!r}")
        alternatives.append((year, month, day))
    if not alternatives:
        raise BatchError("Death date has no alternatives")
    return max(alternatives)


def select_eligible(
    rows: Sequence[dict[str, str]],
    batch_size: int | None,
    target_qids: Sequence[str] | None = None,
    removed_qids: set[str] | None = None,
) -> tuple[list[dict[str, str]], int]:
    if batch_size <= 0:
        raise BatchError("Batch size must be positive")
    removed_qids = removed_qids or set()
    by_qid = {row["wikidata_id"]: row for row in rows}
    if target_qids is not None:
        missing = [
            qid for qid in target_qids if qid not in by_qid and qid not in removed_qids
        ]
        if missing:
            raise BatchError(f"Target QIDs missing from people CSV: {missing}")
        eligible = [
            by_qid[qid]
            for qid in target_qids
            if qid in by_qid
            and qid not in removed_qids
            and not row_is_terminal(by_qid[qid])
        ]
        return eligible[:batch_size], len(eligible)
    eligible = [
        row
        for row in rows
        if row["wikidata_id"] not in removed_qids and not row_is_terminal(row)
    ]
    eligible.sort(key=lambda row: row["wikidata_id"])
    eligible.sort(
        key=lambda row: parse_latest_possible_date(row["death_date"]), reverse=True
    )
    return eligible[:batch_size], len(eligible)


def eligibility_status(
    people_csv: Path,
    limit: int = 3,
    target_manifest: Path | None = None,
    removed_csv: Path | None = None,
) -> dict[str, Any]:
    fields, rows = read_csv(people_csv)
    removed_qids = set()
    if removed_csv is not None:
        _, removed_rows = load_removed_entries(removed_csv, fields)
        removed_qids = {row["wikidata_id"] for row in removed_rows}
    manifest = load_target_manifest(target_manifest) if target_manifest else None
    target_qids = (
        [item["wikidata_id"] for item in manifest["people"]] if manifest else None
    )
    selected, eligible_count = select_eligible(
        rows, max(limit, 1), target_qids, removed_qids
    )
    result = {
        "eligible_count": eligible_count,
        "next": [
            {
                "wikidata_id": row["wikidata_id"],
                "name": row["name"],
                "death_date": row["death_date"],
            }
            for row in selected[: max(limit, 0)]
        ],
    }
    if manifest:
        result["target_manifest"] = str(target_manifest)
        result["target_count"] = len(target_qids or [])
        result["target_remaining"] = eligible_count
    return result


def ambiguous_member_classes(row: Mapping[str, str]) -> list[str]:
    """Return the independent Wikipedia-review classes for one live member."""

    classes = []
    if str(row.get("age_status", "")).strip() == "possible":
        classes.append("possible")
    if len(split_values(row.get("birth_date", ""))) > 1:
        classes.append("multiple_birth_dates")
    if len(split_values(row.get("death_date", ""))) > 1:
        classes.append("multiple_death_dates")
    return classes


def _ambiguous_source_row_sha256(row: Mapping[str, str]) -> str:
    payload = {
        key: str(row.get(key, ""))
        for key in (
            "wikidata_id",
            "name",
            "wikipedia_url",
            "age_status",
            "birth_date",
            "death_date",
        )
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _load_ambiguous_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 2,
            "review_policy_version": AMBIGUOUS_REVIEW_POLICY_VERSION,
            "members": {},
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in {1, 2}
        or not isinstance(value.get("members"), dict)
    ):
        raise BatchError(f"Invalid ambiguous-member state: {path}")
    return value


def _ambiguous_review_row_sha256(row: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json(dict(row)).encode()).hexdigest()


def prepare_ambiguous_repair_manifest(
    *,
    people_csv: Path,
    review_csv: Path,
    state_path: Path,
    output: Path,
    action: str,
    expected_count: int,
) -> dict[str, Any]:
    """Freeze an exact report-action repair target with preservation hashes."""

    if action not in AMBIGUOUS_ACTIONS:
        raise BatchError(f"Invalid repair action: {action}")
    fields, report_rows = read_csv(review_csv)
    if fields != AMBIGUOUS_REVIEW_COLUMNS:
        raise BatchError("Ambiguous-member review CSV schema mismatch")
    _, people_rows = read_csv(people_csv)
    people_by_qid = {row["wikidata_id"]: row for row in people_rows}
    state = _load_ambiguous_state(state_path)
    target = [row for row in report_rows if row["recommended_action"] == action]
    if len(target) != expected_count:
        raise BatchError(
            f"Expected {expected_count} {action!r} rows, found {len(target)}"
        )
    target_records = []
    for row in target:
        qid = row["wikidata_id"]
        person = people_by_qid.get(qid)
        if person is None or not ambiguous_member_classes(person):
            raise BatchError(f"Repair target is not a live ambiguous member: {qid}")
        source_hash = _ambiguous_source_row_sha256(person)
        if source_hash != row["source_row_sha256"]:
            raise BatchError(f"Repair target source row changed: {qid}")
        previous = state["members"].get(qid)
        if not previous or previous.get("source_row_sha256") != source_hash:
            raise BatchError(f"Repair target completion state is stale: {qid}")
        target_records.append(
            {
                "wikidata_id": qid,
                "name": row["name"],
                "source_row_sha256": source_hash,
                "report_row_sha256": _ambiguous_review_row_sha256(row),
                "source_run_dir": row["source_run_dir"],
            }
        )
    target_qids = {item["wikidata_id"] for item in target_records}
    preserved = {
        row["wikidata_id"]: _ambiguous_review_row_sha256(row)
        for row in report_rows
        if row["wikidata_id"] not in target_qids
    }
    manifest = {
        "schema_version": 1,
        "lane": "ambiguous_members_repair",
        "review_policy_version": AMBIGUOUS_REVIEW_POLICY_VERSION,
        "target_action": action,
        "expected_count": expected_count,
        "report_sha256": hashlib.sha256(review_csv.read_bytes()).hexdigest(),
        "state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        "target_rows": target_records,
        "preserved_report_rows": preserved,
        "preserved_state_qids_sha256": hashlib.sha256(
            "\n".join(sorted(set(state["members"]) - target_qids)).encode()
        ).hexdigest(),
        "created_utc": utc_now(),
    }
    atomic_write_json(output, manifest)
    return {
        "manifest": str(output),
        "target_action": action,
        "target_count": len(target_records),
        "preserved_report_rows": len(preserved),
        "report_sha256": manifest["report_sha256"],
        "state_sha256": manifest["state_sha256"],
    }


def _load_ambiguous_repair_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("lane") == "ambiguous_members_mixed_repair":
        required = {
            "schema_version", "lane", "review_policy_version", "created_utc",
            "people_csv", "people_sha256", "report_csv", "report_sha256",
            "state_path", "state_sha256", "target_count", "target_counts",
            "targets", "preserved_report_rows", "preserved_state_members",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value.get("schema_version") != 1
            or value.get("review_policy_version") != AMBIGUOUS_REVIEW_POLICY_VERSION
            or not isinstance(value.get("targets"), list)
            or len(value["targets"]) != value.get("target_count")
            or value.get("target_counts") != {"current_none": 148, "grandfathered_absent": 71}
            or value.get("target_count") != 219
            or not isinstance(value.get("preserved_report_rows"), dict)
            or not isinstance(value.get("preserved_state_members"), dict)
        ):
            raise BatchError(f"Invalid mixed ambiguous-member repair manifest: {path}")
        qids = [str(item.get("wikidata_id", "")) for item in value["targets"]]
        classes = [item.get("target_class") for item in value["targets"]]
        if (
            any(not QID_RE.fullmatch(qid) for qid in qids)
            or len(qids) != len(set(qids))
            or classes.count("current_none") != 148
            or classes.count("grandfathered_absent") != 71
            or any(item.get("target_class") not in {"current_none", "grandfathered_absent"} for item in value["targets"])
        ):
            raise BatchError(f"Invalid mixed repair manifest targets: {path}")
        for item in value["targets"]:
            if item["target_class"] == "current_none":
                if item.get("current_report_action") != "none" or not item.get("report_row_sha256"):
                    raise BatchError(f"Invalid current-none repair target: {item['wikidata_id']}")
            elif item.get("current_report_action") != "absent" or item.get("report_row_sha256") is not None:
                raise BatchError(f"Invalid grandfathered-absent repair target: {item['wikidata_id']}")
            for key in ("source_row_sha256", "state_entry_sha256"):
                if not isinstance(item.get(key), str) or len(item[key]) != 64:
                    raise BatchError(f"Invalid mixed repair target hash: {item['wikidata_id']}")
        return value
    required = {
        "schema_version",
        "lane",
        "review_policy_version",
        "target_action",
        "expected_count",
        "report_sha256",
        "state_sha256",
        "target_rows",
        "preserved_report_rows",
        "preserved_state_qids_sha256",
        "created_utc",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("lane") != "ambiguous_members_repair"
        or value.get("review_policy_version") != AMBIGUOUS_REVIEW_POLICY_VERSION
        or not isinstance(value.get("target_rows"), list)
        or len(value["target_rows"]) != value.get("expected_count")
    ):
        raise BatchError(f"Invalid ambiguous-member repair manifest: {path}")
    qids = [str(item.get("wikidata_id", "")) for item in value["target_rows"]]
    if any(not QID_RE.fullmatch(qid) for qid in qids) or len(qids) != len(set(qids)):
        raise BatchError(f"Invalid repair manifest QIDs: {path}")
    return value


def _mixed_repair_targets(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return target records across legacy and explicit mixed repair manifests."""

    return manifest["targets"] if manifest.get("lane") == "ambiguous_members_mixed_repair" else manifest["target_rows"]


def select_ambiguous_members(
    rows: Sequence[dict[str, str]],
    *,
    batch_size: int | None,
    state: Mapping[str, Any] | None = None,
    rescan_all: bool = False,
) -> tuple[list[dict[str, str]], int, int]:
    """Select live ambiguous members independently of enrichment terminal state."""

    if batch_size is not None and batch_size <= 0:
        raise BatchError("Batch size must be positive")
    processed = (state or {}).get("members", {})
    state_policy_current = (
        (state or {}).get("review_policy_version")
        == AMBIGUOUS_REVIEW_POLICY_VERSION
    )
    eligible = [row for row in rows if ambiguous_member_classes(row)]
    eligible.sort(key=lambda row: (row["wikidata_id"], row["name"].casefold()))
    pending = [
        row
        for row in eligible
        if rescan_all
        or not state_policy_current
        or str(processed.get(row["wikidata_id"], {}).get("source_row_sha256", ""))
        != _ambiguous_source_row_sha256(row)
    ]
    selected = pending if batch_size is None else pending[:batch_size]
    return selected, len(eligible), len(pending)


def ambiguous_member_status(
    *,
    people_csv: Path,
    state_path: Path,
    limit: int = 3,
    rescan_all: bool = False,
) -> dict[str, Any]:
    _, rows = read_csv(people_csv)
    state = _load_ambiguous_state(state_path)
    selected, eligible_count, pending_count = select_ambiguous_members(
        rows,
        batch_size=max(limit, 1),
        state=state,
        rescan_all=rescan_all,
    )
    return {
        "lane": "ambiguous_members",
        "eligible_count": eligible_count,
        "pending_count": pending_count,
        "processed_current_count": eligible_count - pending_count,
        "rescan_all": rescan_all,
        "next": [
            {
                "wikidata_id": row["wikidata_id"],
                "name": row["name"],
                "member_classes": ambiguous_member_classes(row),
            }
            for row in selected[: max(limit, 0)]
        ],
    }


def create_ambiguous_cohort(
    *,
    people_csv: Path,
    cache_root: Path,
    state_path: Path,
    batch_size: int | None,
    run_dir: Path | None = None,
    rescan_all: bool = False,
    target_manifest: Path | None = None,
    review_csv: Path = AMBIGUOUS_REVIEW_CSV,
) -> Path:
    """Freeze an independent cohort spanning all live ambiguous-member states."""

    run_started_utc = utc_now()
    fieldnames, rows = read_csv(people_csv)
    state = _load_ambiguous_state(state_path)
    repair_manifest: dict[str, Any] | None = None
    if target_manifest is not None:
        if batch_size is not None or rescan_all:
            raise BatchError("Target-manifest selection cannot use batch size or rescan-all")
        repair_manifest = _load_ambiguous_repair_manifest(target_manifest)
        if hashlib.sha256(people_csv.read_bytes()).hexdigest() != repair_manifest.get(
            "people_sha256", hashlib.sha256(people_csv.read_bytes()).hexdigest()
        ):
            raise BatchError("People CSV changed after repair freeze")
        if hashlib.sha256(review_csv.read_bytes()).hexdigest() != repair_manifest["report_sha256"]:
            raise BatchError("Ambiguous-member review CSV changed after repair freeze")
        if hashlib.sha256(state_path.read_bytes()).hexdigest() != repair_manifest["state_sha256"]:
            raise BatchError("Ambiguous-member state changed after repair freeze")
        eligible = {row["wikidata_id"]: row for row in rows if ambiguous_member_classes(row)}
        report_fields, report_rows = read_csv(review_csv)
        if report_fields != AMBIGUOUS_REVIEW_COLUMNS:
            raise BatchError("Ambiguous-member review CSV schema mismatch")
        report_by_qid = {row["wikidata_id"]: row for row in report_rows}
        if len(report_by_qid) != len(report_rows):
            raise BatchError("Duplicate QID in ambiguous-member review CSV")
        selected = []
        for item in _mixed_repair_targets(repair_manifest):
            qid = item["wikidata_id"]
            row = eligible.get(qid)
            if row is None or _ambiguous_source_row_sha256(row) != item["source_row_sha256"]:
                raise BatchError(f"Repair target changed or is no longer eligible: {qid}")
            if repair_manifest.get("lane") == "ambiguous_members_mixed_repair":
                state_entry = state["members"].get(qid)
                if state_entry is None or hashlib.sha256(canonical_json(state_entry).encode()).hexdigest() != item["state_entry_sha256"]:
                    raise BatchError(f"Repair target state changed: {qid}")
                report_row = report_by_qid.get(qid)
                if item["target_class"] == "current_none":
                    if report_row is None or report_row["recommended_action"] != "none" or _ambiguous_review_row_sha256(report_row) != item["report_row_sha256"]:
                        raise BatchError(f"Repair target report changed or is ineligible: {qid}")
                elif report_row is not None or state_entry.get("outcome") != "no_reportable_evidence":
                    raise BatchError(f"Grandfathered repair target classification changed: {qid}")
            selected.append(row)
        eligible_count = len(eligible)
        pending_count = len(selected)
    else:
        selected, eligible_count, pending_count = select_ambiguous_members(
            rows,
            batch_size=batch_size,
            state=state,
            rescan_all=rescan_all,
        )
    if not selected:
        raise BatchError("No ambiguous members remain for cohort selection")
    qids = [row["wikidata_id"] for row in selected]
    cohort_hash = hashlib.sha256("\n".join(qids).encode()).hexdigest()
    if run_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = cache_root / "ambiguous-cohorts" / f"{stamp}-{cohort_hash[:12]}"
    if (run_dir / "cohort.json").exists():
        raise BatchError(f"Cohort already exists: {run_dir}")
    selected_records = []
    for ordinal, row in enumerate(selected):
        selected_records.append(
            {
                "ordinal": ordinal,
                "approval_tranche": ordinal // APPROVAL_TRANCHE_SIZE + 1,
                "wikidata_id": row["wikidata_id"],
                "name": row["name"],
                "wikipedia_url": row["wikipedia_url"],
                "age_status": row["age_status"],
                "birth_date": row["birth_date"],
                "death_date": row["death_date"],
                "member_classes": ambiguous_member_classes(row),
                "source_row_sha256": _ambiguous_source_row_sha256(row),
            }
        )
    cohort = {
        "schema_version": 2,
        "lane": "ambiguous_members",
        "run_started_utc": run_started_utc,
        "requested_batch_size": batch_size,
        "selection_mode": (
            "target_manifest"
            if target_manifest is not None
            else "rescan_all" if rescan_all else "pending"
        ),
        "approval_tranche_size": APPROVAL_TRANCHE_SIZE,
        "eligible_count_at_selection": eligible_count,
        "pending_count_at_selection": pending_count,
        "selected_count": len(selected_records),
        "cohort_hash": cohort_hash,
        "public_csv_columns": fieldnames,
        "state_path": str(state_path),
        "selected": selected_records,
        "stages": {"selection_complete_utc": utc_now()},
    }
    if target_manifest is not None and repair_manifest is not None:
        cohort["target_manifest"] = str(target_manifest)
        cohort["target_manifest_sha256"] = hashlib.sha256(
            target_manifest.read_bytes()
        ).hexdigest()
        cohort["target_action"] = repair_manifest.get("target_action", "mixed")
    atomic_write_json(run_dir / "cohort.json", cohort)
    return run_dir


def _require_ambiguous_cohort(value: Path) -> tuple[Path, dict[str, Any]]:
    run_dir, cohort = cohort_paths(value)
    if cohort.get("lane") != "ambiguous_members":
        raise BatchError("Command requires an ambiguous_members cohort")
    return run_dir, cohort


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def protected_snapshot(
    fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> tuple[list[str], dict[str, dict[str, str]], str]:
    protected = [column for column in fieldnames if column not in FALLBACK_COLUMNS]
    snapshot = {
        row["wikidata_id"]: {column: str(row.get(column, "")) for column in protected}
        for row in rows
    }
    digest = hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()
    return protected, snapshot, digest


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_write_csv(
    path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def prepare_redo(
    *,
    people_csv: Path,
    review_csv: Path,
    backup_dir: Path,
    expected_count: int,
    rebuild_browser: bool,
    removed_csv: Path | None = None,
) -> dict[str, Any]:
    if expected_count <= 0:
        raise BatchError("Expected redo count must be positive")
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise BatchError(f"Backup directory is not empty: {backup_dir}")
    fieldnames, rows = read_csv(people_csv)
    removed_qids = set()
    if removed_csv is not None:
        _, removed_rows = load_removed_entries(removed_csv, fieldnames)
        removed_qids = {row["wikidata_id"] for row in removed_rows}
    target_rows = [
        row
        for row in rows
        if row["wikidata_id"] not in removed_qids
        and any(str(row.get(column, "")).strip() for column in FALLBACK_COLUMNS)
    ]
    if len(target_rows) != expected_count:
        raise BatchError(
            f"Redo target count is {len(target_rows)}, expected {expected_count}; no files changed"
        )
    target_qids = [row["wikidata_id"] for row in target_rows]
    if len(target_qids) != len(set(target_qids)):
        raise BatchError("Redo target contains duplicate QIDs")
    target_set = set(target_qids)
    review_fields, review_rows = read_csv(review_csv)
    if review_fields != REVIEW_COLUMNS:
        raise BatchError("Review queue schema mismatch")

    cleared_rows = [dict(row) for row in rows]
    for row in cleared_rows:
        if row["wikidata_id"] in target_set:
            for column in FALLBACK_COLUMNS:
                row[column] = ""
    validate_public_rows(fieldnames, cleared_rows)
    ordered_targets = [row for row in cleared_rows if row["wikidata_id"] in target_set]
    ordered_targets.sort(key=lambda row: row["wikidata_id"])
    ordered_targets.sort(
        key=lambda row: parse_latest_possible_date(row["death_date"]), reverse=True
    )
    globally_selected, _ = select_eligible(
        cleared_rows, expected_count, removed_qids=removed_qids
    )
    if [row["wikidata_id"] for row in globally_selected] != [
        row["wikidata_id"] for row in ordered_targets
    ]:
        raise BatchError("Cleared redo target is not the next exact global cohort")

    created = utc_now()
    manifest = {
        "schema_version": 1,
        "created_utc": created,
        "expected_count": expected_count,
        "source_people_sha256": hashlib.sha256(people_csv.read_bytes()).hexdigest(),
        "source_review_sha256": hashlib.sha256(review_csv.read_bytes()).hexdigest(),
        "expected_cohort_sizes": [
            min(100, expected_count - start)
            for start in range(0, expected_count, 100)
        ],
        "people": [
            {
                "wikidata_id": row["wikidata_id"],
                "name": row["name"],
                "death_date": row["death_date"],
            }
            for row in ordered_targets
        ],
    }
    backup_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(backup_dir / "people_rows.csv", fieldnames, target_rows)
    atomic_write_csv(
        backup_dir / "wikipedia_stronger_model_review.csv",
        review_fields,
        review_rows,
    )
    atomic_write_json(backup_dir / "target-manifest.json", manifest)

    remaining_review = [row for row in review_rows if row["wikidata_id"] not in target_set]
    atomic_write_csv(people_csv, fieldnames, cleared_rows)
    atomic_write_csv(review_csv, review_fields, remaining_review)
    if rebuild_browser:
        subprocess.run([sys.executable, str(BROWSER_BUILDER)], check=True, cwd=REPO_ROOT)
    result = {
        "prepared_utc": created,
        "target_count": expected_count,
        "expected_cohort_sizes": manifest["expected_cohort_sizes"],
        "review_rows_removed": len(review_rows) - len(remaining_review),
        "backup_dir": str(backup_dir),
        "target_manifest": str(backup_dir / "target-manifest.json"),
        "browser_rebuilt": rebuild_browser,
    }
    atomic_write_json(backup_dir / "preparation.json", result)
    return result


def create_cohort(
    *,
    people_csv: Path,
    cache_root: Path,
    batch_size: int,
    run_dir: Path | None = None,
    target_manifest: Path | None = None,
    removed_csv: Path | None = None,
) -> Path:
    run_started_utc = utc_now()
    fieldnames, rows = read_csv(people_csv)
    removed_qids = set()
    if removed_csv is not None:
        _, removed_rows = load_removed_entries(removed_csv, fieldnames)
        removed_qids = {row["wikidata_id"] for row in removed_rows}
    manifest = load_target_manifest(target_manifest) if target_manifest else None
    target_qids = (
        [item["wikidata_id"] for item in manifest["people"]] if manifest else None
    )
    selection_limit = batch_size if batch_size is not None else max(len(rows), 1)
    selected, eligible_count = select_eligible(
        rows, selection_limit, target_qids, removed_qids
    )
    if not selected:
        raise BatchError("No eligible rows remain for cohort selection")
    protected, snapshot, snapshot_hash = protected_snapshot(fieldnames, selected)
    qids = [row["wikidata_id"] for row in selected]
    cohort_hash = hashlib.sha256("\n".join(qids).encode()).hexdigest()
    if run_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = cache_root / "cohorts" / f"{stamp}-{cohort_hash[:12]}"
    if (run_dir / "cohort.json").exists():
        raise BatchError(f"Cohort already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    selected_records = []
    for ordinal, row in enumerate(selected):
        selected_records.append(
            {
                "ordinal": ordinal,
                "approval_tranche": ordinal // APPROVAL_TRANCHE_SIZE + 1,
                "wikidata_id": row["wikidata_id"],
                "name": row["name"],
                "wikipedia_url": row["wikipedia_url"],
                "death_date": row["death_date"],
                "needs": {
                    field: not field_is_effective(row, field)
                    for field in FIELD_SPECS
                },
                "effective_death_fields": {
                    field: effective_labels(row, field)
                    for field in ("cause", "manner")
                },
            }
        )
    cohort = {
        "schema_version": 2,
        "run_started_utc": run_started_utc,
        "requested_batch_size": batch_size,
        "selection_mode": "bounded" if batch_size is not None else "all_eligible",
        "approval_tranche_size": APPROVAL_TRANCHE_SIZE,
        "eligible_count_at_selection": eligible_count,
        "selected_count": len(selected_records),
        "cohort_hash": cohort_hash,
        "target_manifest": str(target_manifest) if target_manifest else None,
        "target_manifest_sha256": (
            hashlib.sha256(target_manifest.read_bytes()).hexdigest()
            if target_manifest
            else None
        ),
        "removed_entries_csv": str(removed_csv) if removed_csv else None,
        "public_csv_columns": fieldnames,
        "protected_columns": protected,
        "protected_snapshot_sha256": snapshot_hash,
        "protected_rows": snapshot,
        "selected": selected_records,
        "stages": {"selection_complete_utc": utc_now()},
    }
    atomic_write_json(run_dir / "cohort.json", cohort)
    return run_dir


def cohort_paths(value: Path) -> tuple[Path, dict[str, Any]]:
    cohort_path = value / "cohort.json" if value.is_dir() else value
    if not cohort_path.exists():
        raise BatchError(f"Missing cohort file: {cohort_path}")
    cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
    if cohort.get("schema_version") not in {1, 2}:
        raise BatchError("Unsupported cohort schema")
    target_manifest = cohort.get("target_manifest")
    if target_manifest:
        manifest_path = Path(str(target_manifest))
        if not manifest_path.exists():
            raise BatchError(f"Target manifest disappeared: {manifest_path}")
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if digest != cohort.get("target_manifest_sha256"):
            raise BatchError("Target manifest changed after cohort selection")
    return cohort_path.parent, cohort


def title_from_url(url: str) -> str:
    marker = "/wiki/"
    if marker not in url:
        raise BatchError(f"Unsupported Wikipedia URL: {url}")
    return urllib.parse.unquote(url.split(marker, 1)[1]).replace("_", " ")


def _retry_after_seconds(value: object, *, default: float) -> float:
    text = str(value or "").strip()
    delay = default
    if text:
        try:
            delay = float(text)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(text)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = default
    return min(max(delay, 1.0), 60.0)


def _decode_response(payload: bytes, encoding: str) -> bytes:
    encoding = encoding.casefold().strip()
    if encoding == "gzip":
        return gzip.decompress(payload)
    if encoding == "deflate":
        try:
            return zlib.decompress(payload)
        except zlib.error:
            return zlib.decompress(payload, -zlib.MAX_WBITS)
    return payload


def _api_json(
    endpoint: str,
    params: Mapping[str, object],
    *,
    retries: int = 2,
    timeout: float = 45,
) -> dict[str, Any]:
    url = endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries + 1):
        HTTP_STATS["requests"] += 1
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = _decode_response(
                    response.read(), response.headers.get("Content-Encoding", "")
                )
                decoded = json.loads(payload)
                error = decoded.get("error") if isinstance(decoded, dict) else None
                if not error:
                    return decoded
                code = str(error.get("code", "unknown"))
                transient = code in TRANSIENT_API_ERRORS or code.startswith(
                    "internal_api_error_"
                )
                if not transient or attempt >= retries:
                    HTTP_STATS["failed_requests"] += 1
                    info = str(error.get("info", "")).strip()
                    raise BatchError(f"MediaWiki API {code}: {info or url}")
                HTTP_STATS["retries"] += 1
                if code == "ratelimited":
                    delay = 30.0
                elif code == "maxlag":
                    delay = _retry_after_seconds(error.get("lag"), default=5.0)
                else:
                    delay = float(2**attempt)
        except urllib.error.HTTPError as exc:
            transient = exc.code == 429 or 500 <= exc.code <= 599
            if not transient or attempt >= retries:
                HTTP_STATS["failed_requests"] += 1
                raise BatchError(f"MediaWiki HTTP {exc.code}: {url}") from exc
            HTTP_STATS["retries"] += 1
            retry_after = exc.headers.get("Retry-After")
            delay = _retry_after_seconds(
                retry_after, default=30.0 if exc.code == 429 else float(2**attempt)
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= retries:
                HTTP_STATS["failed_requests"] += 1
                raise BatchError(f"MediaWiki request failed: {url}: {exc}") from exc
            HTTP_STATS["retries"] += 1
            delay = 2**attempt
        time.sleep(min(delay, 60))
    raise AssertionError("unreachable")


def _resolve_alias(title: str, aliases: Mapping[str, str]) -> str:
    seen: set[str] = set()
    while title in aliases and title not in seen:
        seen.add(title)
        title = aliases[title]
    return title


def parse_pages(
    payload: Mapping[str, Any], requested_titles: Sequence[str]
) -> dict[str, dict[str, Any]]:
    query = payload.get("query", {})
    aliases: dict[str, str] = {}
    for key in ("normalized", "converted", "redirects"):
        for item in query.get(key, []):
            source, target = item.get("from"), item.get("to")
            if source and target:
                aliases[source] = target
    pages = {
        page.get("title"): page
        for page in query.get("pages", [])
        if page.get("title") and not page.get("missing")
    }
    resolved = {}
    for requested in requested_titles:
        title = _resolve_alias(requested, aliases)
        page = pages.get(title)
        if page is None:
            raise BatchError(f"MediaWiki returned no resolved page for {requested!r}")
        resolved[requested] = page
    return resolved


def _cached_article_is_usable(
    cached: Mapping[str, Any], selected: Mapping[str, Any], max_age_hours: float
) -> bool:
    required = {
        "wikidata_id",
        "article_url",
        "language",
        "resolved_title",
        "revision_id",
        "article_bytes",
        "raw_wikitext",
        "fetched_utc",
    }
    if not required.issubset(cached):
        return False
    if cached["wikidata_id"] != selected["wikidata_id"]:
        return False
    if cached["article_url"] != selected["wikipedia_url"]:
        return False
    if not cached["raw_wikitext"]:
        return False
    if re.match(r"^\s*#redirect\b", str(cached["raw_wikitext"]), flags=re.I):
        return False
    try:
        fetched = datetime.fromisoformat(str(cached["fetched_utc"]))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)
    return age.total_seconds() <= max_age_hours * 3600


def fetch_articles(
    *,
    cohort_value: Path,
    cache_root: Path,
    max_cache_age_hours: float = 24,
    batch_limit: int = 20,
) -> dict[str, int]:
    run_dir, cohort = cohort_paths(cohort_value)
    for key in HTTP_STATS:
        HTTP_STATS[key] = 0
    article_root = cache_root / "articles"
    article_root.mkdir(parents=True, exist_ok=True)
    selected = cohort["selected"]
    misses: list[dict[str, Any]] = []
    hits = 0
    for person in selected:
        cache_path = article_root / f"{person['wikidata_id']}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if _cached_article_is_usable(cached, person, max_cache_age_hours):
                hits += 1
                continue
        misses.append(person)

    endpoint = "https://en.wikipedia.org/w/api.php"
    for start in range(0, len(misses), batch_limit):
        batch = misses[start : start + batch_limit]
        titles = [title_from_url(person["wikipedia_url"]) for person in batch]
        payload = _api_json(
            endpoint,
            {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "redirects": 1,
                "converttitles": 1,
                "prop": "revisions",
                "rvprop": "ids|size|content",
                "rvslots": "main",
                "titles": "|".join(titles),
                "maxlag": 5,
            },
        )
        pages = parse_pages(payload, titles)
        for person, requested_title in zip(batch, titles):
            page = pages[requested_title]
            revision = (page.get("revisions") or [{}])[0]
            raw = ((revision.get("slots") or {}).get("main") or {}).get("content")
            if raw is None:
                raw = revision.get("content")
            if not raw or not revision.get("revid"):
                raise BatchError(
                    f"Missing current revision content for {person['wikidata_id']}"
                )
            record = {
                "wikidata_id": person["wikidata_id"],
                "name": person["name"],
                "article_url": person["wikipedia_url"],
                "language": "en",
                "requested_title": requested_title,
                "resolved_title": page["title"],
                "revision_id": revision["revid"],
                "article_bytes": len(raw.encode("utf-8")),
                "raw_wikitext": raw,
                "fetched_utc": utc_now(),
            }
            atomic_write_json(article_root / f"{person['wikidata_id']}.json", record)

    index = []
    total_bytes = 0
    for person in selected:
        record = json.loads(
            (article_root / f"{person['wikidata_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        if not _cached_article_is_usable(record, person, max_cache_age_hours):
            raise BatchError(f"Invalid article cache for {person['wikidata_id']}")
        total_bytes += int(record["article_bytes"])
        index.append(
            {
                key: record[key]
                for key in (
                    "wikidata_id",
                    "name",
                    "article_url",
                    "language",
                    "requested_title",
                    "resolved_title",
                    "revision_id",
                    "article_bytes",
                    "fetched_utc",
                )
            }
        )
    atomic_write_json(run_dir / "article_index.json", index)
    stats = {
        "selected": len(selected),
        "cache_hits": hits,
        "downloaded": len(misses),
        "article_bytes": total_bytes,
        **HTTP_STATS,
    }
    atomic_write_json(run_dir / "fetch_stats.json", stats)
    cohort["stages"]["article_retrieval_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    return stats


def _balanced_template(text: str, pattern: str) -> tuple[str, str]:
    match = re.search(pattern, text, flags=re.I)
    if not match:
        return "", text
    depth = 0
    index = match.start()
    end = None
    while index < len(text) - 1:
        token = text[index : index + 2]
        if token == "{{":
            depth += 1
            index += 2
            continue
        if token == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                end = index
                break
            continue
        index += 1
    if end is None:
        return "", text
    return text[match.start() : end], text[: match.start()] + text[end:]


def _template_visible_text(inner: str) -> str:
    parts = [part.strip() for part in inner.split("|")]
    if not parts:
        return ""
    name = parts[0].casefold().replace("_", " ")
    values: list[str] = []
    citation = name.startswith(("cite ", "citation", "sfn", "harv"))
    preferred = {
        "title",
        "chapter",
        "work",
        "website",
        "publisher",
        "quote",
        "trans-title",
        "author",
        "last",
        "first",
    }
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().casefold().replace("_", "-")
            if citation and key in preferred and value.strip():
                values.append(value.strip())
            elif not citation and key in {
                "text",
                "name",
                "title",
                "reason",
                "cause",
                "occupation",
                "known-for",
            } and value.strip():
                values.append(value.strip())
        elif part and not re.match(r"^https?://", part):
            values.append(part)
    if citation:
        return " ".join(dict.fromkeys(values))
    if name in {
        "lang",
        "nowrap",
        "small",
        "quote",
        "convert",
        "birth date",
        "death date",
        "death date and age",
        "age",
    }:
        return " ".join(values)
    return " ".join(values[:3])


def clean_wikitext(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    # A self-closing ref also matches the opening half of the paired-ref pattern.
    # Remove it first so it cannot consume article prose through a later </ref>.
    text = re.sub(r"<ref\b[^>]*/>", " ", text, flags=re.I)
    text = re.sub(
        r"<ref\b[^>]*>(.*?)</ref\s*>", r" \1 ", text, flags=re.I | re.S
    )
    for _ in range(30):
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            changed = True
            return " " + _template_visible_text(match.group(1)) + " "

        text = re.sub(r"\{\{([^{}]*)\}\}", replace, text)
        if not changed:
            break
    text = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s*([^\]]*)\]", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s*[|!{}]+", " ", text, flags=re.M)
    text = re.sub(r"'{2,5}", "", text)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _first_sentence(paragraph: str) -> tuple[str, str]:
    match = re.search(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])", paragraph)
    if not match:
        return paragraph, ""
    return paragraph[: match.start()].strip(), paragraph[match.end() :].strip()


def _article_sections(body_raw: str) -> list[dict[str, Any]]:
    heading_re = re.compile(r"^(={2,4})\s*([^=].*?)\s*\1\s*$", flags=re.M)
    matches = list(heading_re.finditer(body_raw))
    sections: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body_raw)
        text = clean_wikitext(body_raw[match.end() : end])
        if text:
            sections.append(
                {
                    "heading": clean_wikitext(match.group(2)),
                    "level": len(match.group(1)),
                    "text": text,
                }
            )
    return sections


def build_packet(article: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(article["raw_wikitext"])
    infobox_raw, without_infobox = _balanced_template(
        raw, r"\{\{\s*Infobox\b"
    )
    heading = re.search(r"^==[^=].*?==\s*$", without_infobox, flags=re.M)
    if heading:
        lead_raw = without_infobox[: heading.start()]
        body_raw = without_infobox[heading.start() :]
    else:
        lead_raw, body_raw = without_infobox, ""
    paragraphs = [
        clean_wikitext(part)
        for part in re.split(r"\n\s*\n", lead_raw)
        if clean_wikitext(part)
    ]
    lead_paragraph = paragraphs[0] if paragraphs else ""
    lead_sentence, rest_lead = _first_sentence(lead_paragraph)
    remaining_lead = " ".join(paragraphs[1:])
    sections = _article_sections(body_raw)
    body_text = " ".join(
        f"{section['heading']}: {section['text']}" for section in sections
    )
    rest_article = body_text
    infobox = clean_wikitext(infobox_raw)
    semantic_size = sum(
        len(value)
        for value in (lead_sentence, rest_lead, remaining_lead, infobox, rest_article)
    )
    if not lead_sentence or semantic_size == 0:
        raise BatchError(f"Could not packetize {article['wikidata_id']}")
    return {
        "schema_version": 3,
        "wikidata_id": article["wikidata_id"],
        "name": article["name"],
        "article_url": article["article_url"],
        "language": article["language"],
        "requested_title": article.get("requested_title", article["resolved_title"]),
        "resolved_title": article["resolved_title"],
        "was_redirected": article.get("requested_title", article["resolved_title"])
        != article["resolved_title"],
        "revision_id": article["revision_id"],
        "article_bytes": article["article_bytes"],
        "lead_sentence": lead_sentence,
        "rest_of_lead_paragraph": rest_lead,
        "remaining_lead_section": remaining_lead,
        "infobox": infobox,
        "rest_of_article": rest_article,
        "article_sections": sections,
        "raw_article_cache": f"articles/{article['wikidata_id']}.json",
        "semantic_characters": semantic_size,
    }


def _iter_wikitext_templates(text: str) -> list[str]:
    """Return balanced templates, including nested templates, in source order."""

    stack: list[int] = []
    found: list[tuple[int, str]] = []
    index = 0
    while index < len(text) - 1:
        token = text[index : index + 2]
        if token == "{{":
            stack.append(index)
            index += 2
            continue
        if token == "}}" and stack:
            start = stack.pop()
            found.append((start, text[start : index + 2]))
            index += 2
            continue
        index += 1
    return [raw for _, raw in sorted(found, key=lambda item: item[0])]


def _split_template_parts(raw: str) -> list[str]:
    inner = raw[2:-2] if raw.startswith("{{") and raw.endswith("}}") else raw
    parts: list[str] = []
    start = 0
    template_depth = 0
    link_depth = 0
    index = 0
    while index < len(inner):
        token = inner[index : index + 2]
        if token == "{{":
            template_depth += 1
            index += 2
            continue
        if token == "}}" and template_depth:
            template_depth -= 1
            index += 2
            continue
        if token == "[[":
            link_depth += 1
            index += 2
            continue
        if token == "]]" and link_depth:
            link_depth -= 1
            index += 2
            continue
        if inner[index] == "|" and template_depth == 0 and link_depth == 0:
            parts.append(inner[start:index].strip())
            start = index + 1
        index += 1
    parts.append(inner[start:].strip())
    return parts


def _template_parameters(raw: str) -> tuple[str, list[str], dict[str, str]]:
    parts = _split_template_parts(raw)
    name = (parts[0] if parts else "").casefold().replace("_", " ").strip()
    positional: list[str] = []
    named: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            named[key.strip().casefold().replace("_", " ")] = value.strip()
        else:
            positional.append(part.strip())
    return name, positional, named


def _infobox_fields(infobox_raw: str) -> dict[str, str]:
    _, positional, named = _template_parameters(infobox_raw)
    del positional
    return named


def _citation_titles(raw: str) -> list[str]:
    titles: list[str] = []
    for template in _iter_wikitext_templates(raw):
        name, _, named = _template_parameters(template)
        if not name.startswith(("cite ", "citation")):
            continue
        for key in ("title", "chapter"):
            value = clean_wikitext(named.get(key, ""))
            if value and value not in titles:
                titles.append(value)
    for reference in re.finditer(
        r"<ref\b[^>]*>(.*?)</ref\s*>", raw, flags=re.I | re.S
    ):
        for link in re.finditer(
            r"\[https?://[^\s\]]+\s+([^\]]+)\]", reference.group(1), flags=re.I
        ):
            value = clean_wikitext(link.group(1))
            if value and value not in titles:
                titles.append(value)
    return titles


NON_PROSE_SECTION_HEADINGS = {
    "bibliography",
    "external links",
    "further reading",
    "notes",
    "references",
    "sources",
}


def _without_reference_metadata(raw: str) -> str:
    """Remove citation containers without exposing their metadata as subject text."""

    value = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
    value = re.sub(r"<ref\b[^>]*/>", " ", value, flags=re.I)
    value = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", " ", value, flags=re.I | re.S)
    citation_prefixes = ("cite ", "citation", "sfn", "harv", "efn")
    for template in reversed(_iter_wikitext_templates(value)):
        name, _, _ = _template_parameters(template)
        if name.startswith(citation_prefixes):
            value = value.replace(template, " ")
    return value


def _visible_article_body(raw: str) -> str:
    """Clean biographical prose while excluding refs and bibliography sections."""

    _, without_infobox = _balanced_template(raw, r"\{\{\s*Infobox\b")
    heading_re = re.compile(r"^(={2,4})\s*([^=].*?)\s*\1\s*$", flags=re.M)
    matches = list(heading_re.finditer(without_infobox))
    chunks = [without_infobox[: matches[0].start()] if matches else without_infobox]
    excluded_level: int | None = None
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = clean_wikitext(match.group(2)).casefold()
        if excluded_level is not None and level <= excluded_level:
            excluded_level = None
        if heading in NON_PROSE_SECTION_HEADINGS:
            excluded_level = level
            continue
        if excluded_level is not None and level > excluded_level:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(without_infobox)
        chunks.append(f"{clean_wikitext(match.group(2))}: {without_infobox[match.end():end]}")
    return clean_wikitext(_without_reference_metadata(" ".join(chunks)))


def _date_precision(value: str) -> int:
    match = DATE_RE.fullmatch(value.strip())
    if not match:
        return 0
    return 3 if match.group(3) else 2 if match.group(2) else 1


def _format_date(year: int, month: int | None = None, day: int | None = None) -> str:
    sign = "-" if year < 0 else ""
    year_text = f"{abs(year):04d}"
    if month is None:
        return f"{sign}{year_text}"
    if day is None:
        return f"{sign}{year_text}-{month:02d}"
    return f"{sign}{year_text}-{month:02d}-{day:02d}"


def _valid_date_value(value: str) -> bool:
    match = DATE_RE.fullmatch(value)
    if not match:
        return False
    month = int(match.group(2)) if match.group(2) else None
    day = int(match.group(3)) if match.group(3) else None
    if month is not None and not 1 <= month <= 12:
        return False
    if day is not None and (
        month is None or not 1 <= day <= _month_days(int(match.group(1)), month)
    ):
        return False
    return day is None or month is not None


STANDARD_DEATH_DATE_TEMPLATES = {"death date", "death date and age", "dda"}
STANDARD_DEATH_YEAR_TEMPLATES = {"death year and age"}
GIVEN_AGE_DATE_TEMPLATES = {"death date and given age"}
TEXT_DEATH_DATE_TEMPLATES = {
    "death-date and age",
    "death date and age text",
    "d-da",
}


def _template_date_values(raw: str, field: str) -> list[str]:
    values: list[str] = []
    for template in _iter_wikitext_templates(raw):
        name, positional, _ = _template_parameters(template)
        triples: list[list[str]] = []
        if field == "birth_date" and name.startswith(("birth date", "bda")):
            triples.append(positional[:3])
        elif field == "death_date" and name in STANDARD_DEATH_DATE_TEMPLATES:
            triples.append(positional[:3])
        elif field == "birth_date" and name in {"death date and age", "dda"}:
            triples.append(positional[3:6])
        elif field == "death_date" and name in STANDARD_DEATH_YEAR_TEMPLATES:
            triples.append(positional[:1])
        elif field == "birth_date" and name in STANDARD_DEATH_YEAR_TEMPLATES:
            triples.append(positional[1:2])
        elif field == "death_date" and name in GIVEN_AGE_DATE_TEMPLATES:
            triples.append(positional[:3])
        elif name in TEXT_DEATH_DATE_TEMPLATES and len(positional) >= 2:
            position = 0 if field == "death_date" else 1
            for value in _date_values_from_text(
                positional[position], allow_bare_year=True
            ):
                if value not in values:
                    values.append(value)
        for parts in triples:
            if not parts or not re.fullmatch(r"[+-]?\d+", parts[0] or ""):
                continue
            numbers = [int(parts[0])]
            for part in parts[1:3]:
                if not re.fullmatch(r"\d+", part or ""):
                    break
                numbers.append(int(part))
            value = _format_date(*numbers)
            if _valid_date_value(value) and value not in values:
                values.append(value)
    return [
        value
        for value in values
        if not any(other.startswith(value + "-") for other in values)
    ]


def _date_values_from_text(text: str, *, allow_bare_year: bool) -> list[str]:
    values: list[str] = []

    def add(year: str, month: str | None = None, day: str | None = None) -> None:
        value = _format_date(
            int(year), int(month) if month else None, int(day) if day else None
        )
        if _valid_date_value(value) and value not in values:
            values.append(value)

    for match in re.finditer(
        r"(?<!\d)([+-]?\d{1,4})-(\d{1,2})-(\d{1,2})(?!\d)", text
    ):
        add(match.group(1), match.group(2), match.group(3))
    month_pattern = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
    for match in re.finditer(
        rf"\b(\d{{1,2}})\s+({month_pattern})\.?\s+([+-]?\d{{3,4}})\b",
        text,
        flags=re.I,
    ):
        add(match.group(3), str(MONTH_NUMBERS[match.group(2).casefold()]), match.group(1))
    for match in re.finditer(
        rf"\b({month_pattern})\.?\s+(\d{{1,2}}),?\s+([+-]?\d{{3,4}})\b",
        text,
        flags=re.I,
    ):
        add(match.group(3), str(MONTH_NUMBERS[match.group(1).casefold()]), match.group(2))
    for match in re.finditer(
        rf"\b({month_pattern})\.?\s+([+-]?\d{{3,4}})\b", text, flags=re.I
    ):
        add(match.group(2), str(MONTH_NUMBERS[match.group(1).casefold()]))
    if allow_bare_year:
        cleaned = clean_wikitext(text)
        if re.fullmatch(r"[+-]?\d{1,4}", cleaned):
            add(cleaned)
        for match in re.finditer(
            r"\b(?:born|birth|died|death)\D{0,24}([12]\d{3})\b",
            cleaned,
            flags=re.I,
        ):
            add(match.group(1))
    return [
        value
        for value in values
        if not any(other.startswith(value + "-") for other in values)
    ]


def _expanded_year_alternatives(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"(?<!\d)([12]\d{3})\s*/\s*(\d{2}|[12]\d{3})(?!\d)", text):
        first = int(match.group(1))
        second_text = match.group(2)
        second = (
            int(second_text)
            if len(second_text) == 4
            else (first // 100) * 100 + int(second_text)
        )
        values.extend((_format_date(first), _format_date(second)))
    return list(dict.fromkeys(values))


def _plain_field_date_values(raw: str) -> list[str]:
    text = clean_wikitext(raw)
    values = _date_values_from_text(text, allow_bare_year=True)
    for value in _expanded_year_alternatives(text):
        if value not in values:
            values.append(value)

    gregorian_years = [
        _format_date(int(match.group(1)))
        for match in re.finditer(
            r"(?<!\d)([12]\d{3})\s*(?:A\.?\s*D\.?|C\.?\s*E\.?)\b",
            text,
            flags=re.I,
        )
    ]
    if gregorian_years:
        precise = [value for value in values if _date_precision(value) > 1]
        return list(dict.fromkeys([*precise, *gregorian_years]))

    local_spans = [
        match.span(1)
        for match in re.finditer(
            r"(?<!\d)([12]\d{3})\s*(?:B\.?\s*S\.?|Bikram\s+Samwat|Vikram\s+Samvat)\b",
            text,
            flags=re.I,
        )
    ]
    for match in re.finditer(r"(?<!\d)([12]\d{3})(?![\ds])", text, flags=re.I):
        if any(start <= match.start(1) and match.end(1) <= end for start, end in local_spans):
            continue
        value = _format_date(int(match.group(1)))
        if value not in values:
            values.append(value)
    return [
        value
        for value in values
        if not any(other.startswith(value + "-") for other in values)
    ]


def _date_values_from_field(raw: str, field: str) -> list[str]:
    visible = _without_reference_metadata(raw)
    values = _template_date_values(visible, field)
    plain = visible
    recognized = (
        STANDARD_DEATH_DATE_TEMPLATES
        | STANDARD_DEATH_YEAR_TEMPLATES
        | GIVEN_AGE_DATE_TEMPLATES
        | TEXT_DEATH_DATE_TEMPLATES
    )
    for template in reversed(_iter_wikitext_templates(plain)):
        name, _, _ = _template_parameters(template)
        if name.startswith(("birth date", "bda")) or name in recognized:
            plain = plain.replace(template, " ")
    for value in _plain_field_date_values(plain):
        if value not in values:
            values.append(value)
    if not values:
        for match in re.finditer(
            r"(?<!\d)([12]\d{3})(?![\ds])", clean_wikitext(plain), flags=re.I
        ):
            value = _format_date(int(match.group(1)))
            if value not in values:
                values.append(value)
    return [
        value
        for value in values
        if not any(other.startswith(value + "-") for other in values)
    ]


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _month_days(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap_year(year) else 28
    return 30 if month in {4, 6, 9, 11} else 31


def _date_bounds(value: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    match = DATE_RE.fullmatch(value)
    if not match:
        raise BatchError(f"Invalid date value: {value}")
    year = int(match.group(1))
    if not match.group(2):
        return (year, 1, 1), (year, 12, 31)
    month = int(match.group(2))
    if not match.group(3):
        return (year, month, 1), (year, month, _month_days(year, month))
    day = int(match.group(3))
    return (year, month, day), (year, month, day)


def _age_on(birth: tuple[int, int, int], death: tuple[int, int, int]) -> int:
    return death[0] - birth[0] - (death[1:] < birth[1:])


def _age_bounds(birth: str, death: str) -> tuple[int, int]:
    birth_early, birth_late = _date_bounds(birth)
    death_early, death_late = _date_bounds(death)
    return _age_on(birth_late, death_early), _age_on(birth_early, death_late)


def _context_excerpt(text: str, start: int, end: int, radius: int = 150) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    excerpt = text[left:right].strip()
    return ("…" if left else "") + excerpt + ("…" if right < len(text) else "")


def _sentence_context_excerpt(
    text: str, start: int, end: int, *, preceding: int = 2, following: int = 1
) -> str:
    """Return whole-sentence context without clipping the evidence sentence."""

    boundaries = [0]
    boundaries.extend(
        match.end()
        for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])", text)
    )
    boundaries.append(len(text))
    spans = [
        (boundaries[index], boundaries[index + 1])
        for index in range(len(boundaries) - 1)
    ]
    match_index = next(
        (
            index
            for index, (left, right) in enumerate(spans)
            if left <= start < right or left < end <= right
        ),
        0,
    )
    first = max(0, match_index - preceding)
    last = min(len(spans), match_index + following + 1)
    left = spans[first][0]
    right = spans[last - 1][1]
    excerpt = text[left:right].strip()
    return ("…" if left else "") + excerpt + ("…" if right < len(text) else "")


def _lead_lifespan_date_pairs(packet: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Return role-labeled birth/death pairs from parenthetical lead lifespans.

    Packetization can place a short description, hatnote, or image before the
    actual biographical lead.  Search the complete lead material, but accept
    only the conventional parenthetical ``birth date - death date`` form so
    unrelated lead dates do not acquire deterministic roles.
    """

    lead = " ".join(
        str(packet.get(key, "")).strip()
        for key in ("lead_sentence", "rest_of_lead_paragraph", "remaining_lead_section")
        if str(packet.get(key, "")).strip()
    )
    pairs: list[tuple[str, str, str]] = []
    for match in re.finditer(r"\(([^()]{1,180})\)", lead):
        content = match.group(1).strip()
        parts = re.split(r"\s+[\-–—]\s+", content, maxsplit=1)
        if len(parts) != 2:
            continue
        birth_values = _date_values_from_text(parts[0], allow_bare_year=True)
        death_values = _date_values_from_text(parts[1], allow_bare_year=True)
        if len(birth_values) != 1 or len(death_values) != 1:
            continue
        pair = (birth_values[0], death_values[0], content)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _explicit_body_age_hits(text: str) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int, int]]]:
    """Find explicit singular ages and ranges without scanning arbitrary numbers."""

    range_hits: list[tuple[int, int, int, int]] = []
    range_pattern = re.compile(
        r"\b(?:aged|at\s+(?:the\s+)?age\s+(?:of\s+)?)\s*"
        r"(\d{1,3})\s*(?:or|to|[-–—])\s*(\d{1,3})\b",
        flags=re.I,
    )
    for match in range_pattern.finditer(text):
        low, high = int(match.group(1)), int(match.group(2))
        if 0 <= low <= 125 and 0 <= high <= 125:
            range_hits.append((match.start(), match.end(), low, high))

    singular_hits: list[tuple[int, int, int]] = []
    patterns = (
        re.compile(
            r"\b(?:aged|at\s+(?:the\s+)?age\s+(?:of\s+)?)\s*(\d{1,3})\b",
            flags=re.I,
        ),
        re.compile(
            r"\b(?:died|dead)\s+(?:at\s+)?(?:the\s+age\s+of\s+|age\s+)?"
            r"(\d{1,3})\b",
            flags=re.I,
        ),
        re.compile(r"\b(\d{1,3})\s*(?:years?|yrs?)\s*old\b", flags=re.I),
        re.compile(r"\b(?:was|is)\s+(?:just\s+)?(\d{1,3})(?!\d)", flags=re.I),
    )
    seen_spans: set[tuple[int, int, int]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            age = int(match.group(1))
            hit = (match.start(), match.end(), age)
            if not 0 <= age <= 125 or hit in seen_spans:
                continue
            if any(
                match.start() < right and match.end() > left
                for left, right, _, _ in range_hits
            ):
                continue
            seen_spans.add(hit)
            singular_hits.append(hit)
    singular_hits.sort()
    return singular_hits, range_hits


def _scan_ambiguous_article(
    person: Mapping[str, Any], article: Mapping[str, Any], packet: Mapping[str, Any]
) -> dict[str, Any]:
    raw = str(article["raw_wikitext"])
    infobox_raw, _ = _balanced_template(raw, r"\{\{\s*Infobox\b")
    fields = _infobox_fields(infobox_raw) if infobox_raw else {}
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add(candidate: dict[str, Any]) -> None:
        key = (
            candidate["kind"],
            candidate.get("source"),
            candidate.get("value"),
            candidate.get("excerpt"),
        )
        if key in seen:
            return
        seen.add(key)
        candidate["candidate_id"] = f"C{len(candidates) + 1:03d}"
        candidates.append(candidate)

    infobox_subject_text = " ".join(
        clean_wikitext(_without_reference_metadata(value))
        for value in fields.values()
    )
    explicit_infobox_ages: list[int] = []
    age_range_spans: list[tuple[int, int]] = []
    for match in re.finditer(
        r"\b(?:aged|age\s+at\s+death|death[_ ]age)\s*(?:=|:)?\s*"
        r"(\d{1,3})\s*(?:or|to|[-–—])\s*(\d{1,3})\b",
        infobox_subject_text,
        flags=re.I,
    ):
        low, high = int(match.group(1)), int(match.group(2))
        if not 0 <= low <= 125 or not 0 <= high <= 125:
            continue
        age_range_spans.append(match.span())
        explicit_infobox_ages.extend((low, high))
        add(
            {
                "kind": "age_range",
                "source": "infobox",
                "value": f"{low}-{high}",
                "excerpt": clean_wikitext(match.group(0)),
                "requires_semantic_review": False,
                "deterministic_claim_type": "other",
                "reason": "Infobox states an age range rather than one canonical age.",
            }
        )
    for match in re.finditer(
        r"\b(?:aged|age\s+at\s+death|death[_ ]age)\s*(?:=|:)?\s*(\d{1,3})\b",
        infobox_subject_text,
        flags=re.I,
    ):
        age = int(match.group(1))
        if not 0 <= age <= 125 or any(
            left <= match.start() < right for left, right in age_range_spans
        ):
            continue
        explicit_infobox_ages.append(age)
        add(
            {
                "kind": "age",
                "source": "infobox",
                "value": age,
                "excerpt": _sentence_context_excerpt(
                    infobox_subject_text, match.start(), match.end(), preceding=1, following=1
                ),
                "requires_semantic_review": False,
                "deterministic_claim_type": "age_at_death",
                "reason": "Explicit infobox age-at-death wording.",
            }
        )
    for template in _iter_wikitext_templates(infobox_raw):
        name, positional, _ = _template_parameters(template)
        if name.startswith("death date and given age") and len(positional) >= 4:
            age_text = positional[3].strip()
            if age_text.isdigit() and 0 <= int(age_text) <= 125:
                age = int(age_text)
                explicit_infobox_ages.append(age)
                add(
                    {
                        "kind": "age",
                        "source": "infobox",
                        "value": age,
                        "excerpt": clean_wikitext(template),
                        "requires_semantic_review": False,
                        "deterministic_claim_type": "age_at_death",
                        "reason": "Infobox death-date-and-given-age template supplies one explicit age.",
                    }
                )
            continue
        if not name.startswith(("death date and age", "dda")) or len(positional) < 6:
            continue
        death_values = _template_date_values(template, "death_date")
        birth_values = _template_date_values(template, "birth_date")
        if len(death_values) == len(birth_values) == 1:
            low, high = _age_bounds(birth_values[0], death_values[0])
            if low == high and 0 <= low <= 125:
                explicit_infobox_ages.append(low)
                add(
                    {
                        "kind": "age",
                        "source": "infobox",
                        "value": low,
                        "excerpt": clean_wikitext(template),
                        "requires_semantic_review": False,
                        "deterministic_claim_type": "age_at_death",
                        "reason": "Infobox death-date-and-age template deterministically renders one age.",
                    }
                )
            elif 0 <= low <= high <= 125:
                explicit_infobox_ages.extend((low, high))
                add(
                    {
                        "kind": "age_range",
                        "source": "infobox",
                        "value": f"{low}-{high}",
                        "excerpt": clean_wikitext(template),
                        "requires_semantic_review": False,
                        "deterministic_claim_type": "other",
                        "reason": "Infobox death-date-and-age template renders an ambiguous age range.",
                    }
                )

    body = _visible_article_body(raw)
    body_age_hits, body_age_ranges = _explicit_body_age_hits(body)
    for start, end, low, high in body_age_ranges:
        add(
            {
                "kind": "age_range",
                "source": "article_body",
                "value": f"{low}-{high}",
                "excerpt": _sentence_context_excerpt(body, start, end),
                "requires_semantic_review": False,
                "deterministic_claim_type": "other",
                "reason": "Article prose states an age range rather than one singular age.",
            }
        )
    for start, end, age in body_age_hits:
        add(
            {
                "kind": "age",
                "source": "article_body",
                "value": age,
                "excerpt": _sentence_context_excerpt(body, start, end),
                "requires_semantic_review": True,
            }
        )
    for title in _citation_titles(raw):
        for match in re.finditer(r"(?<!\d)(26|27|28)(?!\d)", title):
            add(
                {
                    "kind": "age",
                    "source": "citation_title",
                    "value": int(match.group(1)),
                    "excerpt": title,
                    "requires_semantic_review": True,
                }
            )

    existing_precision = {
        field: max((_date_precision(value) for value in split_values(person[field])), default=0)
        for field in ("birth_date", "death_date")
    }
    for field in ("birth_date", "death_date"):
        raw_value = fields.get(field.replace("_", " "), "") or fields.get(field, "")
        field_values = _date_values_from_field(raw_value, field)
        field_template_names = {
            _template_parameters(template)[0]
            for template in _iter_wikitext_templates(
                _without_reference_metadata(raw_value)
            )
        }
        structured_field = any(
            name.startswith(("birth date", "bda"))
            or name
            in (
                STANDARD_DEATH_DATE_TEMPLATES
                | STANDARD_DEATH_YEAR_TEMPLATES
                | GIVEN_AGE_DATE_TEMPLATES
                | TEXT_DEATH_DATE_TEMPLATES
            )
            for name in field_template_names
        )
        for value in field_values:
            if _date_precision(value) < existing_precision[field]:
                continue
            add(
                {
                    "kind": "date",
                    "source": "infobox",
                    "value": value,
                    "precision": _date_precision(value),
                    "excerpt": (
                        value
                        if structured_field and len(field_values) == 1
                        else clean_wikitext(_without_reference_metadata(raw_value))
                    ),
                    "requires_semantic_review": False,
                    "deterministic_claim_type": field,
                    "reason": "Explicit labeled infobox date with equal or better precision than Wikidata.",
                }
            )

    for birth, death, excerpt in _lead_lifespan_date_pairs(packet):
        for field, value in (("birth_date", birth), ("death_date", death)):
            if _date_precision(value) < existing_precision[field]:
                continue
            add(
                {
                    "kind": "date",
                    "source": "lead",
                    "value": value,
                    "precision": _date_precision(value),
                    "excerpt": excerpt,
                    "requires_semantic_review": False,
                    "deterministic_claim_type": field,
                    "reason": "Conventional parenthetical lead lifespan supplies a role-labeled date.",
                }
            )

    lead = str(packet.get("lead_sentence", "")).strip()
    for value in _date_values_from_text(lead, allow_bare_year=True):
        if _date_precision(value) < min(existing_precision.values() or [0]):
            continue
        add(
            {
                "kind": "date",
                "source": "lead",
                "value": value,
                "precision": _date_precision(value),
                "excerpt": lead,
                "requires_semantic_review": True,
            }
        )

    unique_infobox_ages = sorted(set(explicit_infobox_ages))
    return {
        "schema_version": 1,
        "lane": "ambiguous_members",
        "wikidata_id": person["wikidata_id"],
        "name": person["name"],
        "article_url": article["article_url"],
        "revision_id": article["revision_id"],
        "member_classes": person["member_classes"],
        "age_status": person["age_status"],
        "wikidata_birth_dates": split_values(person["birth_date"]),
        "wikidata_death_dates": split_values(person["death_date"]),
        "source_row_sha256": person["source_row_sha256"],
        "canonical_infobox_age": (
            unique_infobox_ages[0] if len(unique_infobox_ages) == 1 else None
        ),
        "ambiguous_infobox_ages": (
            unique_infobox_ages if len(unique_infobox_ages) > 1 else []
        ),
        "candidates": candidates,
        "semantic_candidate_ids": [
            candidate["candidate_id"]
            for candidate in candidates
            if candidate["requires_semantic_review"]
        ],
        "pending_semantic_candidate_ids": [
            candidate["candidate_id"]
            for candidate in candidates
            if candidate["requires_semantic_review"]
        ],
        "reused_candidate_reviews": [],
    }


def scan_ambiguous_members(
    *, cohort_value: Path, cache_root: Path
) -> dict[str, Any]:
    run_dir, cohort = _require_ambiguous_cohort(cohort_value)
    candidate_root = run_dir / "ambiguity" / "candidates"
    counts = {"members": 0, "candidates": 0, "semantic_members": 0, "no_hits": 0}
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        article_path = cache_root / "articles" / f"{qid}.json"
        packet_path = run_dir / "packets" / f"{qid}.json"
        if not article_path.exists() or not packet_path.exists():
            raise BatchError(f"Missing article/packet for {qid}; run fetch and packetize first")
        article = json.loads(article_path.read_text(encoding="utf-8"))
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        record = _scan_ambiguous_article(person, article, packet)
        atomic_write_json(candidate_root / f"{qid}.json", record)
        counts["members"] += 1
        counts["candidates"] += len(record["candidates"])
        if record["semantic_candidate_ids"]:
            counts["semantic_members"] += 1
        if not record["candidates"]:
            counts["no_hits"] += 1
    atomic_write_json(run_dir / "ambiguity" / "scan-stats.json", counts)
    cohort["stages"]["ambiguity_scan_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    return counts


def _ambiguous_candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    payload = {
        key: candidate.get(key)
        for key in ("kind", "source", "value", "excerpt")
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _semantic_reuse_safe(
    candidate: Mapping[str, Any], review: Mapping[str, Any]
) -> bool:
    if review.get("verdict") == "ambiguous" or candidate.get("source") == "citation_title":
        return False
    if candidate.get("kind") == "age" and review.get("verdict") == "rejected":
        age = re.escape(str(candidate.get("value", "")))
        if re.search(
            rf"\b(?:he|she|they|[A-Z][\w’'-]+)\s+was\s+(?:just\s+)?{age}\b",
            str(candidate.get("excerpt", "")),
            flags=re.I,
        ):
            return False
    return True


def reuse_ambiguous_reviews(
    *, cohort_value: Path, state_path: Path, source_cohort: Path | None = None
) -> dict[str, int]:
    """Reuse only unchanged, non-ambiguous semantic decisions from prior runs."""

    run_dir, cohort = _require_ambiguous_cohort(cohort_value)
    if "ambiguity_scan_complete_utc" not in cohort.get("stages", {}):
        raise BatchError("Run scan-ambiguous before reusing prior reviews")
    state = _load_ambiguous_state(state_path)
    counts = {
        "members": len(cohort["selected"]),
        "members_fully_reused": 0,
        "members_partially_reused": 0,
        "candidate_reviews_reused": 0,
        "semantic_members_pending": 0,
    }
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        record = _ambiguous_candidate_record(run_dir, qid)
        semantic_output = _ambiguous_semantic_path(run_dir, qid)
        if semantic_output.exists():
            semantic_output.unlink()
        all_ids = list(record["semantic_candidate_ids"])
        if not all_ids:
            continue
        previous_state = state["members"].get(qid, {})
        previous_run = source_cohort or Path(
            str(previous_state.get("source_run_dir", ""))
        )
        old_record_path = previous_run / "ambiguity" / "candidates" / f"{qid}.json"
        old_semantic_path = previous_run / "ambiguity" / "semantic" / f"{qid}.json"
        reusable_by_fingerprint: dict[str, dict[str, Any]] = {}
        reusable_by_key: dict[tuple[Any, ...], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        if old_record_path.exists() and old_semantic_path.exists():
            try:
                old_record = _ambiguous_candidate_record(previous_run, qid)
                old_semantic = _validate_ambiguous_semantic_result(
                    previous_run,
                    qid,
                    json.loads(old_semantic_path.read_text(encoding="utf-8")),
                )
            except (BatchError, json.JSONDecodeError, OSError):
                old_record = {}
                old_semantic = {"candidate_reviews": []}
            old_candidates = {
                candidate["candidate_id"]: candidate
                for candidate in old_record.get("candidates", [])
            }
            for review in old_semantic["candidate_reviews"]:
                candidate = old_candidates.get(review["candidate_id"])
                if candidate and (
                    source_cohort is not None
                    or _semantic_reuse_safe(candidate, review)
                ):
                    reusable_by_fingerprint[
                        _ambiguous_candidate_fingerprint(candidate)
                    ] = review
                    key = (
                        candidate.get("kind"),
                        candidate.get("source"),
                        candidate.get("value"),
                    )
                    reusable_by_key.setdefault(key, []).append((candidate, review))

        new_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for candidate in record["candidates"]:
            if candidate["candidate_id"] not in all_ids:
                continue
            key = (
                candidate.get("kind"),
                candidate.get("source"),
                candidate.get("value"),
            )
            new_by_key.setdefault(key, []).append(candidate)
        ordinal_reuse: dict[str, dict[str, Any]] = {}
        for key, new_candidates in new_by_key.items():
            old_candidates = reusable_by_key.get(key, [])
            if len(old_candidates) != len(new_candidates):
                continue
            for new_candidate, (_, old_review) in zip(new_candidates, old_candidates):
                if source_cohort is not None or _semantic_reuse_safe(
                    new_candidate, old_review
                ):
                    ordinal_reuse[new_candidate["candidate_id"]] = old_review

        reused: list[dict[str, Any]] = []
        pending: list[str] = []
        for candidate in record["candidates"]:
            candidate_id = candidate["candidate_id"]
            if candidate_id not in all_ids:
                continue
            old_review = reusable_by_fingerprint.get(
                _ambiguous_candidate_fingerprint(candidate)
            ) or ordinal_reuse.get(candidate_id)
            if old_review is None:
                pending.append(candidate_id)
                continue
            reused.append({**old_review, "candidate_id": candidate_id})
        record["pending_semantic_candidate_ids"] = pending
        record["reused_candidate_reviews"] = reused
        atomic_write_json(
            run_dir / "ambiguity" / "candidates" / f"{qid}.json", record
        )
        counts["candidate_reviews_reused"] += len(reused)
        if pending:
            counts["semantic_members_pending"] += 1
            if reused:
                counts["members_partially_reused"] += 1
        else:
            combined = {
                "schema_version": 1,
                "wikidata_id": qid,
                "candidate_reviews": reused,
                "reason": "All semantic decisions were reused from unchanged prior evidence.",
            }
            normalized = _validate_ambiguous_semantic_result(run_dir, qid, combined)
            atomic_write_json(semantic_output, normalized)
            counts["members_fully_reused"] += 1
    atomic_write_json(run_dir / "ambiguity" / "reuse-stats.json", counts)
    cohort["stages"]["ambiguity_reuse_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    return counts


def packetize_articles(*, cohort_value: Path, cache_root: Path) -> dict[str, int]:
    run_dir, cohort = cohort_paths(cohort_value)
    packet_root = run_dir / "packets"
    packet_root.mkdir(parents=True, exist_ok=True)
    total = 0
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        article_path = cache_root / "articles" / f"{qid}.json"
        if not article_path.exists():
            raise BatchError(f"Missing article cache for {qid}; run fetch first")
        article = json.loads(article_path.read_text(encoding="utf-8"))
        packet = build_packet(article)
        total += int(packet["semantic_characters"])
        atomic_write_json(packet_root / f"{qid}.json", packet)
    stats = {"packets": len(cohort["selected"]), "semantic_characters": total}
    atomic_write_json(run_dir / "packet_stats.json", stats)
    cohort["stages"]["packet_extraction_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    return stats


SEMANTIC_ROLES = (
    "eligibility",
    "alternate-eligibility",
    "death-evidence",
    "death-classification",
    "identity",
)
TASK_STATES = {"running", "failed"}
SCHEDULER_ROLES = (*SEMANTIC_ROLES, "vocabulary")
MAX_SEMANTIC_SLOTS = 6
APPROVAL_TRANCHE_SIZE = 100
MAX_TASK_ATTEMPTS = 3  # initial assignment plus two diagnosed recoveries
ASSIGNMENT_LIMITS = {
    "eligibility": {"max_bytes": 256 * 1024, "max_items": 20},
    "alternate-eligibility": {"max_bytes": 256 * 1024, "max_items": 20},
    "death-evidence": {"max_bytes": 160 * 1024, "max_items": 15},
    "death-classification": {"max_bytes": 64 * 1024, "max_items": 20},
    "identity": {"max_bytes": 32 * 1024, "max_items": 20},
    "vocabulary": {"max_bytes": 32 * 1024, "max_items": 12},
}


def semantic_artifact_path(run_dir: Path, role: str, qid: str) -> Path:
    if role not in SEMANTIC_ROLES:
        raise BatchError(f"Unknown semantic role: {role}")
    if not QID_RE.fullmatch(qid):
        raise BatchError(f"Invalid semantic artifact QID: {qid!r}")
    return run_dir / "semantic" / role / f"{qid}.json"


def _load_semantic_artifact(run_dir: Path, role: str, qid: str) -> dict[str, Any]:
    path = semantic_artifact_path(run_dir, role, qid)
    if not path.exists():
        raise BatchError(f"{qid}: missing {role} artifact: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchError(f"{qid}: invalid JSON in {role} artifact") from exc
    if not isinstance(value, dict):
        raise BatchError(f"{qid}: {role} artifact is not an object")
    if value.get("schema_version") != 1 or value.get("wikidata_id") != qid:
        raise BatchError(f"{qid}: invalid {role} artifact identity/schema")
    return value


def record_semantic_artifact(
    *,
    cohort_value: Path,
    role: str,
    qid: str,
    input_path: Path,
    _scheduler_install: bool = False,
) -> Path:
    """Install one worker artifact exactly once into its owned role/QID path."""
    run_dir, cohort = cohort_paths(cohort_value)
    if cohort.get("selection_mode") == "all_eligible" and not _scheduler_install:
        raise BatchError(
            "All-eligible queues install semantic results only through complete-assignment"
        )
    selected_qids = {person["wikidata_id"] for person in cohort["selected"]}
    if qid not in selected_qids:
        raise BatchError(f"Artifact QID is outside the cohort: {qid}")
    try:
        value = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchError(f"Invalid semantic artifact JSON: {input_path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("wikidata_id") != qid
    ):
        raise BatchError("Semantic artifact identity/schema does not match destination")
    destination = semantic_artifact_path(run_dir, role, qid)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise BatchError(f"Semantic artifact already exists: {destination}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _reject_out_of_cohort_artifacts(run_dir: Path, selected_qids: set[str]) -> None:
    for role in SEMANTIC_ROLES:
        role_root = run_dir / "semantic" / role
        if not role_root.exists():
            continue
        extras = sorted(path.stem for path in role_root.glob("*.json") if path.stem not in selected_qids)
        if extras:
            raise BatchError(f"Out-of-cohort {role} artifacts: {extras}")


def mark_semantic_task(
    *, cohort_value: Path, role: str, qid: str, status: str, reason: str = ""
) -> Path:
    """Record controller-owned running/failed state for scheduling visibility."""
    run_dir, cohort = cohort_paths(cohort_value)
    if qid not in {person["wikidata_id"] for person in cohort["selected"]}:
        raise BatchError(f"Task QID is outside the cohort: {qid}")
    if role not in SEMANTIC_ROLES or status not in TASK_STATES:
        raise BatchError("Invalid semantic task role/status")
    if status == "failed" and not reason.strip():
        raise BatchError("Failed semantic task requires a reason")
    current = next(
        item for item in stage_status(cohort_value=cohort_value)["people"]
        if item["wikidata_id"] == qid
    )["roles"][role]
    if status == "running" and current not in {"ready", "running", "failed"}:
        raise BatchError(f"{qid}: {role} is not ready (current state: {current})")
    if status == "failed" and current not in {"ready", "running", "failed"}:
        raise BatchError(f"{qid}: {role} cannot fail from state {current}")
    path = run_dir / "semantic" / "task-status" / role / f"{qid}.json"
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "wikidata_id": qid,
            "role": role,
            "status": status,
            "reason": reason.strip(),
            "updated_utc": utc_now(),
        },
    )
    return path


def _validate_semantic_page_review(
    qid: str, candidate_id: str, value: object
) -> dict[str, str]:
    required = {
        "page_kind",
        "subject_match",
        "subject_is_human",
        "life_status",
        "age_compatibility",
        "reason",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise BatchError(f"{qid}: malformed eligibility review for {candidate_id}")
    review = {key: str(value[key]).strip() for key in required}
    if review["page_kind"] not in PAGE_KINDS:
        raise BatchError(f"{qid}: invalid page kind for {candidate_id}")
    if review["subject_match"] not in SUBJECT_MATCHES:
        raise BatchError(f"{qid}: invalid subject match for {candidate_id}")
    if review["subject_is_human"] not in HUMAN_STATUSES:
        raise BatchError(f"{qid}: invalid human status for {candidate_id}")
    if review["life_status"] not in LIFE_STATUSES:
        raise BatchError(f"{qid}: invalid life status for {candidate_id}")
    if review["age_compatibility"] not in AGE_COMPATIBILITIES:
        raise BatchError(f"{qid}: invalid age compatibility for {candidate_id}")
    if not review["reason"]:
        raise BatchError(f"{qid}: blank eligibility reason for {candidate_id}")
    return review


def _load_eligibility_reviews(
    run_dir: Path, qid: str, role: str = "eligibility"
) -> dict[str, dict[str, str]]:
    artifact = _load_semantic_artifact(run_dir, role, qid)
    if set(artifact) != {"schema_version", "wikidata_id", "reviews"}:
        raise BatchError(f"{qid}: malformed {role} artifact")
    reviews = artifact["reviews"]
    if not isinstance(reviews, dict) or not reviews:
        raise BatchError(f"{qid}: {role} artifact needs at least one review")
    if role == "eligibility" and set(reviews) != {"enwiki"}:
        raise BatchError(f"{qid}: primary eligibility artifact must contain only enwiki")
    if role == "alternate-eligibility" and "enwiki" in reviews:
        raise BatchError(f"{qid}: alternate eligibility cannot contain enwiki")
    return {
        str(candidate_id): _validate_semantic_page_review(qid, str(candidate_id), review)
        for candidate_id, review in reviews.items()
    }


def _page_is_eligible(review: Mapping[str, str]) -> bool:
    return (
        review["page_kind"] == "person"
        and review["subject_match"] == "match"
        and review["subject_is_human"] != "nonhuman"
        and review["life_status"] not in {"living", "conflicting"}
        and review["age_compatibility"] not in {"outside_26_28", "conflicting"}
    )


def _candidate_packet(
    run_dir: Path,
    qid: str,
    candidate_id: str,
    candidates: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if candidate_id == "enwiki":
        packet_path = run_dir / "packets" / f"{qid}.json"
    else:
        item = candidates.get(candidate_id)
        if item is None:
            raise BatchError(f"{qid}: unknown article candidate {candidate_id}")
        packet_path = run_dir / str(item["packet"])
    if not packet_path.exists():
        raise BatchError(f"{qid}: selected article packet is missing: {packet_path}")
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchError(f"{qid}: invalid article packet: {packet_path}") from exc
    if packet.get("schema_version") != 3 or packet.get("wikidata_id") != qid:
        raise BatchError(f"{qid}: invalid article packet identity/schema")
    return packet


def _archived_article_candidates(
    *,
    run_dir: Path,
    qid: str,
    candidate_ids: Iterable[str],
    candidates: Mapping[str, Mapping[str, Any]],
    archived_urls: set[str],
) -> list[str]:
    matches: list[str] = []
    for candidate_id in candidate_ids:
        packet = _candidate_packet(run_dir, qid, candidate_id, candidates)
        if _normalize_enwiki_article_url(packet.get("article_url")) in archived_urls:
            matches.append(candidate_id)
    return matches


def _deterministic_removal_reasons(
    reviews: Mapping[str, Mapping[str, str]],
) -> list[str]:
    person_reviews = [review for review in reviews.values() if review["page_kind"] == "person"]
    reasons: list[str] = []
    if not person_reviews:
        reasons.append("no_dedicated_person_article")
        return reasons
    matching = [review for review in person_reviews if review["subject_match"] == "match"]
    if not matching:
        reasons.append("subject_identity_mismatch")
        return reasons
    if any(review["subject_is_human"] == "nonhuman" for review in matching):
        reasons.append("nonhuman")
    if any(review["life_status"] in {"living", "conflicting"} for review in matching):
        reasons.append("living")
    if any(
        review["age_compatibility"] in {"outside_26_28", "conflicting"}
        for review in matching
    ):
        reasons.append("age_outside_26_28")
    return reasons or ["subject_identity_mismatch"]


def aggregate_eligibility(
    *, cohort_value: Path, ready_only: bool = False
) -> dict[str, Any]:
    """Validate independent eligibility artifacts and select articles deterministically."""
    run_dir, cohort = cohort_paths(cohort_value)
    _reject_out_of_cohort_artifacts(
        run_dir, {person["wikidata_id"] for person in cohort["selected"]}
    )
    alternate_index_path = run_dir / "alternate_article_index.json"
    alternate_index = (
        json.loads(alternate_index_path.read_text(encoding="utf-8"))
        if alternate_index_path.exists()
        else {}
    )
    approved_musician_qids = _load_approved_musician_qids()
    archived_27_club_urls = _load_archived_27_club_urls()
    selection_path = run_dir / "semantic" / "article-selection.json"
    people: dict[str, dict[str, Any]] = {}
    if ready_only and selection_path.exists():
        previous = json.loads(selection_path.read_text(encoding="utf-8"))
        if isinstance(previous.get("people"), dict):
            people.update(previous["people"])
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        if qid in people:
            continue
        primary_path = semantic_artifact_path(run_dir, "eligibility", qid)
        if ready_only and not primary_path.exists():
            continue
        reviews = _load_eligibility_reviews(run_dir, qid)
        candidates = {
            str(item["candidate_id"]): item for item in alternate_index.get(qid, [])
        }
        stay_overrides: list[str] = []
        if _page_is_eligible(reviews["enwiki"]):
            selected_article = "enwiki"
            decision = "eligible"
            removal_reasons: list[str] = []
            packet = f"packets/{qid}.json"
        else:
            if ready_only and qid not in alternate_index:
                continue
            if candidates:
                alternate_path = semantic_artifact_path(
                    run_dir, "alternate-eligibility", qid
                )
                if ready_only and not alternate_path.exists():
                    continue
                alternate_reviews = _load_eligibility_reviews(
                    run_dir, qid, "alternate-eligibility"
                )
                reviews.update(alternate_reviews)
            expected = {"enwiki", *candidates}
            if set(reviews) != expected:
                missing = sorted(expected - set(reviews))
                extra = sorted(set(reviews) - expected)
                raise BatchError(
                    f"{qid}: alternate eligibility mismatch; missing={missing}, extra={extra}"
                )
            qualifying = [
                candidate_id
                for candidate_id in candidates
                if _page_is_eligible(reviews[candidate_id])
            ]
            if qualifying:
                selected_article = min(
                    qualifying,
                    key=lambda candidate_id: (
                        -int(candidates[candidate_id]["article_bytes"]), candidate_id
                    ),
                )
                decision = "eligible"
                removal_reasons = []
                packet = str(candidates[selected_article]["packet"])
            else:
                selected_article = ""
                decision = "possible_removal"
                removal_reasons = _deterministic_removal_reasons(reviews)
                packet = f"packets/{qid}.json"
                stay_overrides = []

                # These rules override only the no-dedicated-person-article
                # reason. Living, nonhuman, age-conflicting, and identity-
                # mismatched subjects remain possible removals.
                if removal_reasons == ["no_dedicated_person_article"]:
                    override_candidates: list[str] = []
                    if qid in approved_musician_qids:
                        stay_overrides.append("approved_musician_occupation")
                        override_candidates.append("enwiki")
                    archived_candidates = _archived_article_candidates(
                        run_dir=run_dir,
                        qid=qid,
                        candidate_ids=("enwiki",),
                        candidates=candidates,
                        archived_urls=archived_27_club_urls,
                    )
                    if archived_candidates:
                        stay_overrides.append("archived_27_club_article")
                        override_candidates.extend(archived_candidates)
                    if override_candidates:
                        selected_article = min(
                            set(override_candidates),
                            key=lambda candidate_id: (
                                -int(
                                    _candidate_packet(
                                        run_dir, qid, candidate_id, candidates
                                    )["article_bytes"]
                                ),
                                candidate_id,
                            ),
                        )
                        decision = "eligible"
                        removal_reasons = []
                        packet = (
                            f"packets/{qid}.json"
                            if selected_article == "enwiki"
                            else str(candidates[selected_article]["packet"])
                        )
                        stay_overrides = sorted(set(stay_overrides))
        packet_path = run_dir / packet
        if not packet_path.exists():
            raise BatchError(f"{qid}: selected packet is missing: {packet_path}")
        people[qid] = {
            "decision": decision,
            "selected_article": selected_article,
            "removal_reasons": removal_reasons,
            "stay_overrides": stay_overrides,
            "packet": packet,
            "reviews": reviews,
        }
    output = {
        "schema_version": 1,
        "generated_utc": utc_now(),
        "complete": len(people) == len(cohort["selected"]),
        "people": {qid: people[qid] for qid in sorted(people)},
    }
    atomic_write_json(selection_path, output)
    return output


def _load_article_selection(
    run_dir: Path, cohort: Mapping[str, Any], *, require_complete: bool = True
) -> dict[str, Any]:
    path = run_dir / "semantic" / "article-selection.json"
    if not path.exists():
        raise BatchError("Run aggregate-eligibility before downstream semantic work")
    value = json.loads(path.read_text(encoding="utf-8"))
    selected_qids = {person["wikidata_id"] for person in cohort["selected"]}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("people"), dict)
        or not set(value["people"]).issubset(selected_qids)
        or (require_complete and set(value["people"]) != selected_qids)
    ):
        raise BatchError("Invalid or stale article-selection artifact")
    return value


def _selected_packet(run_dir: Path, cohort: Mapping[str, Any], qid: str) -> dict[str, Any]:
    selection = _load_article_selection(run_dir, cohort, require_complete=False)
    if qid not in selection["people"]:
        raise BatchError(f"{qid}: article selection is not ready")
    selected = selection["people"][qid]
    if selected["decision"] != "eligible":
        raise BatchError(f"{qid}: possible-removal rows have no downstream role input")
    packet_path = run_dir / str(selected["packet"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if packet.get("schema_version") != 3 or packet.get("wikidata_id") != qid:
        raise BatchError(f"{qid}: invalid selected packet")
    return packet


def build_role_input(
    *,
    cohort_value: Path,
    role: str,
    qid: str,
    output: Path | None = None,
    vocabulary_path: Path = APPROVED_VOCABULARY,
) -> Path:
    """Build the narrow deterministic input for one semantic role."""
    run_dir, cohort = cohort_paths(cohort_value)
    people = {person["wikidata_id"]: person for person in cohort["selected"]}
    if qid not in people:
        raise BatchError(f"Role input QID is outside the cohort: {qid}")
    person = people[qid]
    common = {
        "schema_version": 1,
        "role": role,
        "wikidata_id": qid,
        "subject": {key: person[key] for key in ("name", "death_date")},
    }
    if role == "eligibility":
        packet_path = run_dir / "packets" / f"{qid}.json"
        if not packet_path.exists():
            raise BatchError(f"{qid}: packetize before building eligibility input")
        value = {**common, "candidates": {"enwiki": json.loads(packet_path.read_text())}}
    elif role == "alternate-eligibility":
        index_path = run_dir / "alternate_article_index.json"
        if not index_path.exists():
            raise BatchError("Fetch alternates before building alternate input")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        items = index.get(qid, [])
        if not items:
            raise BatchError(f"{qid}: no alternate candidates")
        value = {
            **common,
            "candidates": {
                item["candidate_id"]: json.loads(
                    (run_dir / str(item["packet"])).read_text(encoding="utf-8")
                )
                for item in items
            },
        }
    elif role == "death-evidence":
        value = {**common, "selected_packet": _selected_packet(run_dir, cohort, qid)}
    elif role == "death-classification":
        evidence = _validate_death_evidence(run_dir, qid)
        mappings = _seed_vocabulary(TRUSTED_VOCABULARY, vocabulary_path)
        value = {
            **common,
            "evidence_bundle": evidence,
            "source_precedence": DEATH_REVIEW_TIERS,
            "needed_fields": {
                field: bool(person["needs"][field]) for field in ("cause", "manner")
            },
            "existing_effective_fields": person.get(
                "effective_death_fields", {"cause": [], "manner": []}
            ),
            "canonical_labels": {
                field: sorted(
                    {item["label"] for item in mappings[field].values()},
                    key=str.casefold,
                )
                for field in ("cause", "manner")
            },
            "status_definitions": {
                "settled": "any mentioned, speculative, reported, suspected, probable, pending, inferred, or competing account",
                "unknown": "neither cause nor manner has any possible account, or both are explicitly unknown or undisclosed without a theory",
            },
        }
    elif role == "identity":
        if not person["needs"]["occupation"]:
            raise BatchError(f"{qid}: identity is already effective")
        packet = _selected_packet(run_dir, cohort, qid)
        value = {
            **common,
            "lead": {
                key: packet[key]
                for key in (
                    "lead_sentence",
                    "rest_of_lead_paragraph",
                    "remaining_lead_section",
                    "infobox",
                )
            },
        }
    else:
        raise BatchError(f"Unsupported role input: {role}")
    if output is None:
        output = run_dir / "agent-inputs" / role / f"{qid}.json"
    atomic_write_json(output, value)
    return output


def _validate_death_evidence(run_dir: Path, qid: str) -> dict[str, Any]:
    artifact = _load_semantic_artifact(run_dir, "death-evidence", qid)
    required = {"schema_version", "wikidata_id", "evidence", "no_usable_account", "reason"}
    if set(artifact) != required:
        raise BatchError(f"{qid}: malformed death-evidence artifact")
    no_usable = artifact["no_usable_account"]
    if (
        not isinstance(no_usable, dict)
        or set(no_usable) != {"cause", "manner"}
        or any(not isinstance(no_usable[field], bool) for field in no_usable)
    ):
        raise BatchError(f"{qid}: no_usable_account must contain cause/manner booleans")
    if not isinstance(artifact["reason"], str) or not artifact["reason"].strip():
        raise BatchError(f"{qid}: blank death-evidence reason")
    evidence = artifact["evidence"]
    if not isinstance(evidence, list):
        raise BatchError(f"{qid}: death evidence must be a list")
    ids: list[str] = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {
            "evidence_id", "source_tier", "section", "text"
        }:
            raise BatchError(f"{qid}: malformed death evidence item")
        evidence_id = str(item["evidence_id"]).strip()
        if not re.fullmatch(r"E[1-9][0-9]*", evidence_id):
            raise BatchError(f"{qid}: invalid death evidence ID {evidence_id!r}")
        if str(item["source_tier"]).strip() not in SOURCE_TIERS - {"none"}:
            raise BatchError(f"{qid}: invalid evidence source tier")
        if not str(item["text"]).strip():
            raise BatchError(f"{qid}: blank death evidence text")
        ids.append(evidence_id)
    if len(ids) != len(set(ids)):
        raise BatchError(f"{qid}: duplicate death evidence IDs")
    if not all(no_usable.values()) and not evidence:
        raise BatchError(f"{qid}: death-evidence artifact is empty")
    return artifact


def _validate_semantic_pairs(qid: str, field: str, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise BatchError(f"{qid}: required {field} classification is empty")
    pairs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "qid"}:
            raise BatchError(f"{qid}: malformed {field} classification")
        label = str(item["label"]).strip()
        item_qid = str(item["qid"]).strip()
        if not label or (item_qid and not QID_RE.fullmatch(item_qid)):
            raise BatchError(f"{qid}: invalid {field} label/QID")
        pairs.append({"label": label, "qid": item_qid})
    if len({pair["label"].casefold() for pair in pairs}) != len(pairs):
        raise BatchError(f"{qid}: duplicate {field} labels")
    if any(pair["label"] == "somevalue" for pair in pairs) and pairs != [
        {"label": "somevalue", "qid": ""}
    ]:
        raise BatchError(f"{qid}: somevalue must be the sole {field} value")
    return sorted(pairs, key=lambda pair: pair["label"].casefold())


def _validate_death_classification(
    run_dir: Path,
    qid: str,
    needs: Mapping[str, bool],
    evidence: Mapping[str, Any],
    effective_death_fields: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    artifact = _load_semantic_artifact(run_dir, "death-classification", qid)
    required = {
        "schema_version", "wikidata_id", "cause", "manner", "status",
        "evidence_ids", "reason",
    }
    if set(artifact) != required:
        raise BatchError(f"{qid}: malformed death-classification artifact")
    status = str(artifact["status"]).strip()
    if status not in {"settled", "unknown"}:
        raise BatchError(f"{qid}: invalid death classification status")
    if not isinstance(artifact["reason"], str) or not artifact["reason"].strip():
        raise BatchError(f"{qid}: blank death-classification reason")
    evidence_ids = artifact["evidence_ids"]
    if not isinstance(evidence_ids, dict) or set(evidence_ids) != {"cause", "manner"}:
        raise BatchError(f"{qid}: malformed classification evidence IDs")
    available_ids = {item["evidence_id"] for item in evidence["evidence"]}
    parsed: dict[str, Any] = dict(artifact)
    for field in ("cause", "manner"):
        refs = evidence_ids[field]
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise BatchError(f"{qid}: malformed {field} evidence ID list")
        if len(refs) != len(set(refs)) or not set(refs).issubset(available_ids):
            raise BatchError(f"{qid}: unknown or duplicate {field} evidence ID")
        if needs[field]:
            parsed[field] = _validate_semantic_pairs(qid, field, artifact[field])
        elif artifact[field] is not None or refs:
            raise BatchError(f"{qid}: already-effective {field} must be null and uncited")
    field_unknown: dict[str, bool] = {}
    for field in ("cause", "manner"):
        if needs[field]:
            is_somevalue = parsed[field] == [{"label": "somevalue", "qid": ""}]
            if is_somevalue and not evidence["no_usable_account"][field]:
                raise BatchError(
                    f"{qid}: {field} somevalue requires no usable account"
                )
            if not is_somevalue and not evidence_ids[field]:
                raise BatchError(
                    f"{qid}: concrete {field} classification requires evidence"
                )
            field_unknown[field] = is_somevalue
            continue
        field_unknown[field] = any(
            str(label).casefold() == "somevalue"
            for label in (effective_death_fields or {}).get(field, [])
        )
    death_unknown = all(field_unknown.values())
    if (status == "unknown") != death_unknown:
        raise BatchError(
            f"{qid}: unknown status must exactly match no usable death account"
        )
    return parsed


def _validate_identity(run_dir: Path, qid: str) -> dict[str, Any]:
    artifact = _load_semantic_artifact(run_dir, "identity", qid)
    required = {"schema_version", "wikidata_id", "occupations", "reason"}
    if set(artifact) != required:
        raise BatchError(f"{qid}: malformed identity artifact")
    if not isinstance(artifact["reason"], str) or not artifact["reason"].strip():
        raise BatchError(f"{qid}: blank identity reason")
    artifact["occupations"] = _validate_semantic_pairs(
        qid, "occupation", artifact["occupations"]
    )
    return artifact


def _validate_completed_role(
    run_dir: Path, person: Mapping[str, Any], role: str
) -> None:
    qid = str(person["wikidata_id"])
    if role in {"eligibility", "alternate-eligibility"}:
        _load_eligibility_reviews(run_dir, qid, role)
    elif role == "death-evidence":
        _validate_death_evidence(run_dir, qid)
    elif role == "death-classification":
        evidence = _validate_death_evidence(run_dir, qid)
        _validate_death_classification(
            run_dir,
            qid,
            person["needs"],
            evidence,
            person.get("effective_death_fields"),
        )
    elif role == "identity":
        _validate_identity(run_dir, qid)


def stage_status(*, cohort_value: Path) -> dict[str, Any]:
    """Report artifact readiness without changing cohort state."""
    run_dir, cohort = cohort_paths(cohort_value)
    selection_path = run_dir / "semantic" / "article-selection.json"
    selection: dict[str, Any] | None = None
    if selection_path.exists():
        try:
            selection = _load_article_selection(
                run_dir, cohort, require_complete=False
            )
        except (BatchError, json.JSONDecodeError):
            selection = None
    people = []
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        decision = (
            selection["people"].get(qid, {}).get("decision") if selection else None
        )
        roles: dict[str, str] = {}
        for role in SEMANTIC_ROLES:
            path = semantic_artifact_path(run_dir, role, qid)
            if path.exists():
                try:
                    _validate_completed_role(run_dir, person, role)
                except (BatchError, json.JSONDecodeError, OSError):
                    roles[role] = "failed"
                else:
                    roles[role] = "complete"
                continue
            task_path = run_dir / "semantic" / "task-status" / role / f"{qid}.json"
            if task_path.exists():
                try:
                    task = json.loads(task_path.read_text(encoding="utf-8"))
                    if (
                        task.get("schema_version") != 1
                        or task.get("wikidata_id") != qid
                        or task.get("role") != role
                    ):
                        raise ValueError("task identity mismatch")
                    task_state = task["status"]
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    task_state = "failed"
                if task_state in TASK_STATES:
                    roles[role] = task_state
                    continue
                roles[role] = "failed"
                continue
            if role == "eligibility":
                roles[role] = "ready"
            elif role == "alternate-eligibility":
                alternate_index_path = run_dir / "alternate_article_index.json"
                has_alternates = False
                if alternate_index_path.exists():
                    index = json.loads(alternate_index_path.read_text(encoding="utf-8"))
                    has_alternates = bool(index.get(qid))
                roles[role] = "ready" if has_alternates else "not_required"
            elif decision != "eligible":
                roles[role] = "not_required" if decision == "possible_removal" else "blocked"
            elif role == "death-evidence":
                roles[role] = "ready"
            elif role == "identity":
                roles[role] = "ready" if person["needs"]["occupation"] else "not_required"
            else:
                roles[role] = (
                    "ready" if roles.get("death-evidence") == "complete" else "blocked"
                )
        people.append({"wikidata_id": qid, "decision": decision, "roles": roles})
    return {
        "cohort": str(run_dir),
        "article_selection": (
            "complete"
            if selection and len(selection["people"]) == len(cohort["selected"])
            else "partial"
            if selection
            else "missing_or_invalid"
        ),
        "people": people,
    }


def _scheduler_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "scheduler" / "state.json"
    if not path.exists():
        return {
            "schema_version": 1,
            "next_assignment": 1,
            "active_leases": {},
            "attempts": {},
            "completed_assignments": [],
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "next_assignment",
        "active_leases",
        "attempts",
        "completed_assignments",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1:
        raise BatchError("Invalid scheduler state")
    return value


def _scheduler_exceptions(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = run_dir / "scheduler" / "exceptions.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BatchError("Invalid scheduler exception lane")
    return value


def _exception_qids(
    exceptions: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    qids = {
        str(item.get("key", ""))
        for item in exceptions.values()
        if item.get("role") in SEMANTIC_ROLES
        and QID_RE.fullmatch(str(item.get("key", "")))
    }
    for item in exceptions.values():
        qids.update(
            str(qid)
            for qid in item.get("affected_qids", [])
            if QID_RE.fullmatch(str(qid))
        )
    return qids


def _scheduler_key(role: str, item_key: str) -> str:
    return f"{role}:{item_key}"


def _vocabulary_ready_items(run_dir: Path) -> list[dict[str, str]]:
    paths = [run_dir / "unresolved_vocabulary.json"]
    paths.extend(sorted((run_dir / "tranches").glob("*/unresolved_vocabulary.json")))
    ready: dict[str, dict[str, str]] = {}
    for unresolved_path in paths:
        candidates_path = unresolved_path.with_name("vocabulary_candidates.json")
        if not unresolved_path.exists() or not candidates_path.exists():
            continue
        unresolved = json.loads(unresolved_path.read_text(encoding="utf-8"))
        for item in unresolved:
            field = str(item["field"])
            label = str(item["label"])
            key = f'{field}:{item["normalized_label"]}'
            if not vocabulary_artifact_path(run_dir, field, label).exists():
                ready.setdefault(
                    key,
                    {
                        "field": field,
                        "label": label,
                        "key": key,
                        "proposals": str(unresolved_path.with_name("proposals.jsonl")),
                    },
                )
    return list(ready.values())


def _vocabulary_affected_qids(run_dir: Path, field: str, label: str) -> list[str]:
    normalized = normalize_vocabulary_label(label)
    paths = [run_dir / "proposals.jsonl"]
    paths.extend(sorted((run_dir / "tranches").glob("*/proposals.jsonl")))
    affected: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for proposal in load_proposals(path):
            if any(
                normalize_vocabulary_label(str(pair.get("label", ""))) == normalized
                for pair in proposal.get(field) or []
            ):
                affected.add(str(proposal["wikidata_id"]))
    return sorted(affected)


def scheduler_status(*, cohort_value: Path) -> dict[str, Any]:
    """Report deterministic queue, lease, exception, and tranche state."""
    run_dir, cohort = cohort_paths(cohort_value)
    state = _scheduler_state(run_dir)
    exceptions = _scheduler_exceptions(run_dir)
    exception_qids = _exception_qids(exceptions)
    stages = stage_status(cohort_value=cohort_value)
    active_keys = {
        key
        for lease in state["active_leases"].values()
        for key in lease.get("task_keys", [])
    }
    ready = {role: [] for role in SCHEDULER_ROLES}
    person_by_qid = {
        person["wikidata_id"]: person for person in cohort["selected"]
    }
    for item in stages["people"]:
        qid = item["wikidata_id"]
        for role in SEMANTIC_ROLES:
            key = _scheduler_key(role, qid)
            role_state = item["roles"][role]
            if (
                role_state in {"ready", "failed"}
                and qid not in exception_qids
                and key not in active_keys
                and key not in exceptions
                and int(state["attempts"].get(key, 0)) < MAX_TASK_ATTEMPTS
            ):
                ready[role].append(qid)
    for item in _vocabulary_ready_items(run_dir):
        key = _scheduler_key("vocabulary", item["key"])
        if (
            key not in active_keys
            and key not in exceptions
            and int(state["attempts"].get(key, 0)) < MAX_TASK_ATTEMPTS
        ):
            ready["vocabulary"].append(item["key"])

    selection_people: Mapping[str, Any] = {}
    selection_path = run_dir / "semantic" / "article-selection.json"
    if selection_path.exists():
        value = _load_article_selection(run_dir, cohort, require_complete=False)
        selection_people = value["people"]
    migrated_qids: set[str] = set()
    for migration_path in sorted((run_dir / "scheduler" / "migrations").glob("*.json")):
        migration = json.loads(migration_path.read_text(encoding="utf-8"))
        migrated_qids.update(str(qid) for qid in migration.get("qids", []))
    tranches: dict[int, dict[str, Any]] = {}
    for qid, person in person_by_qid.items():
        number = int(person.get("approval_tranche", 1))
        tranche = tranches.setdefault(
            number,
            {
                "tranche": number,
                "total": 0,
                "ready": 0,
                "exceptions": 0,
                "migrated": 0,
            },
        )
        tranche["total"] += 1
        if qid in migrated_qids:
            tranche["migrated"] += 1
            continue
        qid_exception = qid in exception_qids
        if qid_exception:
            tranche["exceptions"] += 1
            continue
        decision = selection_people.get(qid, {}).get("decision")
        roles = next(row["roles"] for row in stages["people"] if row["wikidata_id"] == qid)
        if decision == "possible_removal" or (
            decision == "eligible"
            and roles["death-evidence"] == "complete"
            and roles["death-classification"] == "complete"
            and roles["identity"] in {"complete", "not_required"}
        ):
            tranche["ready"] += 1
    for tranche in tranches.values():
        tranche["reviewable"] = (
            tranche["ready"] + tranche["exceptions"] + tranche["migrated"]
            == tranche["total"]
        )
    frozen_qids = {person["wikidata_id"] for person in cohort["selected"]}
    processing_remaining = sorted(frozen_qids - migrated_qids - exception_qids)

    rejected_without_alternates = []
    alternate_index_path = run_dir / "alternate_article_index.json"
    alternate_index = (
        json.loads(alternate_index_path.read_text(encoding="utf-8"))
        if alternate_index_path.exists()
        else {}
    )
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        if qid in selection_people or qid in alternate_index:
            continue
        if semantic_artifact_path(run_dir, "eligibility", qid).exists():
            reviews = _load_eligibility_reviews(run_dir, qid)
            if not _page_is_eligible(reviews["enwiki"]):
                rejected_without_alternates.append(qid)
    actions = []
    if rejected_without_alternates:
        actions.append("fetch-alternates")
    if any(
        semantic_artifact_path(run_dir, "eligibility", person["wikidata_id"]).exists()
        and person["wikidata_id"] not in selection_people
        and (
            _page_is_eligible(
                _load_eligibility_reviews(run_dir, person["wikidata_id"])["enwiki"]
            )
            or person["wikidata_id"] in alternate_index
        )
        for person in cohort["selected"]
    ):
        actions.append("aggregate-ready")
    usage_path = run_dir / "scheduler" / "usage.json"
    usage_records = (
        json.loads(usage_path.read_text(encoding="utf-8"))
        if usage_path.exists()
        else []
    )
    usage_by_role: dict[str, dict[str, int]] = {}
    for record in usage_records:
        summary = usage_by_role.setdefault(
            record["role"],
            {
                "assignments": 0,
                "items": 0,
                "serialized_input_bytes": 0,
                "recorded_input_tokens": 0,
                "recorded_output_tokens": 0,
                "token_records": 0,
            },
        )
        summary["assignments"] += 1
        summary["items"] += int(record["items"])
        summary["serialized_input_bytes"] += int(record["serialized_input_bytes"])
        if record.get("recorded_input_tokens") is not None:
            summary["recorded_input_tokens"] += int(record["recorded_input_tokens"])
            summary["recorded_output_tokens"] += int(record.get("recorded_output_tokens") or 0)
            summary["token_records"] += 1
    return {
        "cohort": str(run_dir),
        "slot_limit": MAX_SEMANTIC_SLOTS,
        "active_leases": state["active_leases"],
        "ready_counts": {role: len(items) for role, items in ready.items()},
        "ready_items": ready,
        "exceptions": list(exceptions.values()),
        "corrections": [
            item for item in exceptions.values() if item.get("role") == "correction"
        ],
        "tranches": [tranches[number] for number in sorted(tranches)],
        "control_actions": actions,
        "article_selection": stages["article_selection"],
        "usage_by_role": usage_by_role,
        "frozen_count": len(frozen_qids),
        "migrated_count": len(frozen_qids & migrated_qids),
        "excepted_qid_count": len(frozen_qids & exception_qids),
        "processing_remaining": len(processing_remaining),
        "processing_complete": (
            not processing_remaining and not state["active_leases"]
        ),
        "completion_scope": "frozen_cohort_only",
    }


def claim_assignment(
    *,
    cohort_value: Path,
    slot: int,
    vocabulary_path: Path = APPROVED_VOCABULARY,
) -> dict[str, Any]:
    """Claim one homogeneous, byte-capped assignment for a refillable slot."""
    if slot not in range(1, MAX_SEMANTIC_SLOTS + 1):
        raise BatchError(f"Slot must be 1..{MAX_SEMANTIC_SLOTS}")
    run_dir, cohort = cohort_paths(cohort_value)
    state = _scheduler_state(run_dir)
    slot_key = str(slot)
    if slot_key in state["active_leases"]:
        raise BatchError(f"Slot {slot} already has an active lease")
    if len(state["active_leases"]) >= MAX_SEMANTIC_SLOTS:
        raise BatchError("All semantic slots are leased")

    # Incorporate every locally ready article selection before choosing work.
    if any(
        semantic_artifact_path(run_dir, "eligibility", person["wikidata_id"]).exists()
        for person in cohort["selected"]
    ):
        aggregate_eligibility(cohort_value=cohort_value, ready_only=True)
    status = scheduler_status(cohort_value=cohort_value)
    ready = status["ready_items"]
    active_roles = {lease["role"] for lease in state["active_leases"].values()}
    # Finish the earliest still-open approval tranche when its identity work
    # is ready.  Identity is normally downstream of the reserved eligibility
    # lane, but leaving a tranche's final identity rows behind a large global
    # eligibility backlog makes that tranche appear permanently stalled.
    pending_tranches = [
        item["tranche"] for item in status["tranches"] if not item["reviewable"]
    ]
    earliest_pending_tranche = min(pending_tranches) if pending_tranches else None
    tranche_by_qid = {
        person["wikidata_id"]: int(person.get("approval_tranche", 1))
        for person in cohort["selected"]
    }
    priority_identity_ready = (
        earliest_pending_tranche is not None
        and any(
            tranche_by_qid.get(qid) == earliest_pending_tranche
            for qid in ready["identity"]
        )
    )
    if priority_identity_ready:
        role = "identity"
    elif ready["eligibility"] and "eligibility" not in active_roles:
        role = "eligibility"
    else:
        priority = (
            "vocabulary",
            "death-classification",
            "death-evidence",
            "identity",
            "alternate-eligibility",
            "eligibility",
        )
        role = next((candidate for candidate in priority if ready[candidate]), "")
    if not role:
        return {"assignment": None, **status}

    limits = ASSIGNMENT_LIMITS[role]
    vocabulary_items = {item["key"]: item for item in _vocabulary_ready_items(run_dir)}
    packed: list[dict[str, Any]] = []
    total_bytes = 0
    for item_key in ready[role]:
        if role == "vocabulary":
            item = vocabulary_items[item_key]
            input_path = build_vocabulary_input(
                proposals_path=Path(item["proposals"]),
                field=item["field"],
                label=item["label"],
            )
        else:
            input_path = build_role_input(
                cohort_value=cohort_value,
                role=role,
                qid=item_key,
                vocabulary_path=vocabulary_path,
            )
        size = input_path.stat().st_size
        if packed and (
            len(packed) >= limits["max_items"]
            or total_bytes + size > limits["max_bytes"]
        ):
            break
        packed.append(
            {
                "key": item_key,
                "input": str(input_path),
                "input_bytes": size,
                "oversize_singleton": not packed and size > limits["max_bytes"],
            }
        )
        total_bytes += size
        if size > limits["max_bytes"]:
            break

    assignment_id = f'A{int(state["next_assignment"]):06d}'
    manifest = {
        "schema_version": 1,
        "assignment_id": assignment_id,
        "slot": slot,
        "role": role,
        "prompt": str(PROJECT_DIR / "semantic-prompts" / (
            "article-eligibility.md"
            if role in {"eligibility", "alternate-eligibility"}
            else f"{role}-resolution.md" if role == "vocabulary" else f"{role}.md"
        )),
        "items": packed,
        "input_bytes": total_bytes,
        "max_input_bytes": limits["max_bytes"],
        "max_items": limits["max_items"],
        "result_format": "JSON array containing exactly one result object per item",
        "claimed_utc": utc_now(),
    }
    manifest_path = run_dir / "scheduler" / "assignments" / f"{assignment_id}.json"
    atomic_write_json(manifest_path, manifest)
    task_keys = []
    for item in packed:
        key = _scheduler_key(role, item["key"])
        task_keys.append(key)
        state["attempts"][key] = int(state["attempts"].get(key, 0)) + 1
        if role != "vocabulary":
            mark_semantic_task(
                cohort_value=cohort_value,
                role=role,
                qid=item["key"],
                status="running",
            )
    state["next_assignment"] += 1
    state["active_leases"][slot_key] = {
        "assignment_id": assignment_id,
        "role": role,
        "manifest": str(manifest_path),
        "task_keys": task_keys,
    }
    atomic_write_json(run_dir / "scheduler" / "state.json", state)
    return {"assignment": str(manifest_path), **manifest}


def _load_assignment_result(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise BatchError("Assignment result must be a JSON object, array, or JSONL")
    return value


def complete_assignment(
    *,
    cohort_value: Path,
    assignment_id: str,
    input_path: Path | None,
    failed_reason: str = "",
    recorded_input_tokens: int | None = None,
    recorded_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Release a lease, preserving valid results and routing exhausted work aside."""
    run_dir, _ = cohort_paths(cohort_value)
    state = _scheduler_state(run_dir)
    leases = [
        (slot, lease)
        for slot, lease in state["active_leases"].items()
        if lease.get("assignment_id") == assignment_id
    ]
    if len(leases) != 1:
        raise BatchError(f"Assignment is not actively leased: {assignment_id}")
    slot, lease = leases[0]
    manifest_path = Path(str(lease["manifest"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(
        value is not None and value < 0
        for value in (recorded_input_tokens, recorded_output_tokens)
    ):
        raise BatchError("Recorded token counts cannot be negative")
    expected = {item["key"]: item for item in manifest["items"]}
    results: list[dict[str, Any]] = []
    parse_error = ""
    if input_path is not None:
        try:
            results = _load_assignment_result(input_path)
        except (BatchError, json.JSONDecodeError, OSError) as exc:
            parse_error = str(exc)
    elif not failed_reason.strip():
        raise BatchError("Completion requires --input or --failed-reason")

    by_key: dict[str, dict[str, Any]] = {}
    duplicate_keys: set[str] = set()
    for result in results:
        if manifest["role"] == "vocabulary":
            field = str(result.get("field", ""))
            label = str(result.get("label", ""))
            key = f"{field}:{normalize_vocabulary_label(label)}"
        else:
            key = str(result.get("wikidata_id", ""))
        if key in by_key:
            duplicate_keys.add(key)
            continue
        by_key[key] = result
    extras = sorted(set(by_key) - set(expected))

    installed: list[str] = []
    failed: list[dict[str, str]] = []
    exceptions = _scheduler_exceptions(run_dir)
    for key in expected:
        task_key = _scheduler_key(manifest["role"], key)
        reason = (
            failed_reason.strip()
            or parse_error
            or (f"duplicate result key: {key}" if key in duplicate_keys else "")
            or "assignment omitted this item"
        )
        result = by_key.get(key)
        if result is not None and key not in duplicate_keys and not parse_error:
            received = run_dir / "scheduler" / "received" / assignment_id / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"
            atomic_write_json(received, result)
            try:
                if manifest["role"] == "vocabulary":
                    record_vocabulary_artifact(
                        cohort_value=cohort_value,
                        input_path=received,
                        _scheduler_install=True,
                    )
                    if result.get("decision") == "no_adequate_candidate":
                        raise BatchError("no_adequate_candidate")
                else:
                    record_semantic_artifact(
                        cohort_value=cohort_value,
                        role=manifest["role"],
                        qid=key,
                        input_path=received,
                        _scheduler_install=True,
                    )
                    person = next(
                        person
                        for person in cohort_paths(cohort_value)[1]["selected"]
                        if person["wikidata_id"] == key
                    )
                    _validate_completed_role(run_dir, person, manifest["role"])
            except (BatchError, json.JSONDecodeError, OSError) as exc:
                if manifest["role"] == "vocabulary":
                    vocabulary_artifact_path(
                        run_dir,
                        str(result.get("field", "")),
                        str(result.get("label", "")),
                    ).unlink(missing_ok=True)
                else:
                    semantic_artifact_path(
                        run_dir, manifest["role"], key
                    ).unlink(missing_ok=True)
                reason = str(exc)
            else:
                installed.append(key)
                continue
        attempts = int(state["attempts"].get(task_key, 0))
        failure = {"key": key, "reason": reason, "attempts": str(attempts)}
        failed.append(failure)
        immediate = reason == "no_adequate_candidate"
        if immediate or attempts >= MAX_TASK_ATTEMPTS:
            exception = {
                "task_key": task_key,
                "role": manifest["role"],
                "key": key,
                "reason": reason,
                "attempts": attempts,
                "entered_utc": utc_now(),
            }
            if manifest["role"] == "vocabulary":
                vocabulary_item = next(
                    (
                        item
                        for item in _vocabulary_ready_items(run_dir)
                        if item["key"] == key
                    ),
                    None,
                )
                field = str(
                    result.get("field", "")
                    if result is not None
                    else (vocabulary_item or {}).get("field", "")
                )
                label = str(
                    result.get("label", "")
                    if result is not None
                    else (vocabulary_item or {}).get("label", "")
                )
                exception["affected_qids"] = _vocabulary_affected_qids(
                    run_dir, field, label
                )
            exceptions[task_key] = exception
        elif manifest["role"] != "vocabulary":
            mark_semantic_task(
                cohort_value=cohort_value,
                role=manifest["role"],
                qid=key,
                status="failed",
                reason=reason,
            )
    state["active_leases"].pop(slot)
    state["completed_assignments"].append(assignment_id)
    atomic_write_json(run_dir / "scheduler" / "state.json", state)
    atomic_write_json(run_dir / "scheduler" / "exceptions.json", exceptions)
    usage_path = run_dir / "scheduler" / "usage.json"
    usage = (
        json.loads(usage_path.read_text(encoding="utf-8"))
        if usage_path.exists()
        else []
    )
    usage.append(
        {
            "assignment_id": assignment_id,
            "role": manifest["role"],
            "items": len(expected),
            "serialized_input_bytes": manifest["input_bytes"],
            "result_bytes": input_path.stat().st_size if input_path and input_path.exists() else None,
            "recorded_input_tokens": recorded_input_tokens,
            "recorded_output_tokens": recorded_output_tokens,
            "completed_utc": utc_now(),
        }
    )
    atomic_write_json(usage_path, usage)
    return {
        "assignment_id": assignment_id,
        "installed": installed,
        "failed": failed,
        "exceptions_added": [
            item for item in failed
            if _scheduler_key(manifest["role"], item["key"]) in exceptions
        ],
        "rejected_extra_keys": extras,
        "slot_released": int(slot),
    }


def resolve_exception(
    *, cohort_value: Path, role: str, key: str, reason: str
) -> dict[str, Any]:
    """Return one diagnosed exception to its last durable scheduling stage."""
    if not reason.strip():
        raise BatchError("Resolving an exception requires a reason")
    run_dir, _ = cohort_paths(cohort_value)
    exceptions = _scheduler_exceptions(run_dir)
    task_key = _scheduler_key(role, key)
    if task_key not in exceptions:
        raise BatchError(f"Exception does not exist: {task_key}")
    resolved = exceptions.pop(task_key)
    atomic_write_json(run_dir / "scheduler" / "exceptions.json", exceptions)
    state = _scheduler_state(run_dir)
    state["attempts"][task_key] = 0
    atomic_write_json(run_dir / "scheduler" / "state.json", state)
    if role == "vocabulary":
        unresolved_paths = [run_dir / "unresolved_vocabulary.json"]
        unresolved_paths.extend(
            sorted((run_dir / "tranches").glob("*/unresolved_vocabulary.json"))
        )
        for unresolved_path in unresolved_paths:
            unresolved = (
                json.loads(unresolved_path.read_text(encoding="utf-8"))
                if unresolved_path.exists()
                else []
            )
            for item in unresolved:
                item_key = f'{item["field"]}:{item["normalized_label"]}'
                if item_key == key:
                    vocabulary_artifact_path(
                        run_dir, item["field"], item["label"]
                    ).unlink(missing_ok=True)
    elif role in SEMANTIC_ROLES and QID_RE.fullmatch(key):
        semantic_artifact_path(run_dir, role, key).unlink(missing_ok=True)
    elif role != "correction":
        raise BatchError(f"Unsupported exception role: {role}")
    atomic_write_json(
        run_dir / "scheduler" / "resolved-exceptions" / f"{hashlib.sha256(task_key.encode()).hexdigest()[:16]}.json",
        {**resolved, "resolution_reason": reason.strip(), "resolved_utc": utc_now()},
    )
    return {"resolved": task_key, "catch_up_approval_required": True}


def assemble_semantic_proposals(
    *, cohort_value: Path, output: Path | None = None, tranche: int | None = None
) -> dict[str, Any]:
    """Serially assemble strict per-QID artifacts into proposal JSONL."""
    run_dir, cohort = cohort_paths(cohort_value)
    _reject_out_of_cohort_artifacts(
        run_dir, {person["wikidata_id"] for person in cohort["selected"]}
    )
    selection = _load_article_selection(
        run_dir, cohort, require_complete=tranche is None
    )
    exceptions = _scheduler_exceptions(run_dir)
    exception_qids = _exception_qids(exceptions)
    migrated_qids: set[str] = set()
    if tranche is not None:
        for migration_path in sorted(
            (run_dir / "scheduler" / "migrations").glob("*.json")
        ):
            migration = json.loads(migration_path.read_text(encoding="utf-8"))
            migrated_qids.update(str(qid) for qid in migration.get("qids", []))
    selected_people = [
        person
        for person in cohort["selected"]
        if (tranche is None or int(person.get("approval_tranche", 1)) == tranche)
        and person["wikidata_id"] not in migrated_qids
        and person["wikidata_id"] not in exception_qids
    ]
    if tranche is not None and not selected_people:
        raise BatchError(f"Approval tranche {tranche} has no ready non-exception QIDs")
    records: list[dict[str, Any]] = []
    for person in selected_people:
        qid = person["wikidata_id"]
        if qid not in selection["people"]:
            raise BatchError(f"{qid}: tranche article selection is not ready")
        selected = selection["people"][qid]
        reviews = selected["reviews"]
        article_eligibility = {
            "decision": selected["decision"],
            "selected_article": selected["selected_article"],
            "primary_review": reviews["enwiki"],
            "alternate_reviews": {
                key: value for key, value in reviews.items() if key != "enwiki"
            },
            "removal_reasons": selected["removal_reasons"],
        }
        if selected.get("stay_overrides"):
            article_eligibility["stay_overrides"] = selected["stay_overrides"]
        if selected["decision"] == "possible_removal":
            records.append(
                {
                    "proposal_schema_version": 2,
                    "wikidata_id": qid,
                    "cause": None,
                    "manner": None,
                    "occupation": None,
                    "status": "possible_removal",
                    "article_eligibility": article_eligibility,
                    "evidence_basis": {field: None for field in FIELD_SPECS},
                    "source_anomalies": [],
                }
            )
            continue
        evidence = _validate_death_evidence(run_dir, qid)
        classification = _validate_death_classification(
            run_dir,
            qid,
            person["needs"],
            evidence,
            person.get("effective_death_fields"),
        )
        identity = _validate_identity(run_dir, qid) if person["needs"]["occupation"] else None
        evidence_basis = {
            field: (
                {
                    "evidence_ids": classification["evidence_ids"][field],
                    "reason": classification["reason"],
                }
                if person["needs"][field]
                else None
            )
            for field in ("cause", "manner")
        }
        evidence_basis["occupation"] = (
            {"evidence_ids": [], "reason": identity["reason"]} if identity else None
        )
        records.append(
            {
                "proposal_schema_version": 2,
                "wikidata_id": qid,
                "cause": classification["cause"] if person["needs"]["cause"] else None,
                "manner": classification["manner"] if person["needs"]["manner"] else None,
                "occupation": identity["occupations"] if identity else None,
                "status": classification["status"],
                "article_eligibility": article_eligibility,
                "evidence_basis": evidence_basis,
                "source_anomalies": [],
            }
        )
    if output is None:
        output = (
            run_dir / "proposals.jsonl"
            if tranche is None
            else run_dir / "tranches" / f"{tranche:03d}" / "proposals.jsonl"
        )
    write_proposals(output, records)
    return {"output": str(output), "assembled": len(records)}


def _article_review_template() -> dict[str, Any]:
    return {
        "decision": "",
        "selected_article": "",
        "primary_review": {
            "page_kind": "",
            "subject_is_human": "",
            "life_status": "",
            "age_compatibility": "",
            "reason": "",
        },
        "alternate_reviews": {},
        "removal_reasons": [],
    }


def fetch_alternate_articles(
    *,
    cohort_value: Path,
    proposals_path: Path | None,
    cache_root: Path,
    batch_limit: int = 20,
) -> dict[str, int]:
    """Fetch every non-English sitelink for primary pages rejected as non-person pages."""
    run_dir, cohort = cohort_paths(cohort_value)
    selected = {person["wikidata_id"]: person for person in cohort["selected"]}
    index_path = run_dir / "alternate_article_index.json"
    existing_index = (
        json.loads(index_path.read_text(encoding="utf-8"))
        if index_path.exists()
        else {}
    )
    requested: list[str] = []
    if proposals_path is not None:
        proposals = {
            item["wikidata_id"]: item for item in load_proposals(proposals_path)
        }
        for qid in selected:
            review = proposals.get(qid, {}).get("article_eligibility", {})
            primary = review.get("primary_review", {}) if isinstance(review, dict) else {}
            if primary.get("page_kind") in PAGE_KINDS - {"person"}:
                requested.append(qid)
    else:
        for qid in selected:
            if qid in existing_index:
                continue
            if not semantic_artifact_path(run_dir, "eligibility", qid).exists():
                continue
            reviews = _load_eligibility_reviews(run_dir, qid)
            if set(reviews) != {"enwiki"}:
                raise BatchError(
                    f"{qid}: pre-fetch eligibility artifact must contain only enwiki"
                )
            if not _page_is_eligible(reviews["enwiki"]):
                requested.append(qid)
    if not requested:
        atomic_write_json(index_path, existing_index)
        stats = {"requested_people": 0, "alternate_articles": 0, "article_bytes": 0}
        atomic_write_json(run_dir / "alternate_fetch_stats.json", stats)
        return stats

    sitelinks: dict[str, list[dict[str, str]]] = {qid: [] for qid in requested}
    endpoint = "https://www.wikidata.org/w/api.php"
    for start in range(0, len(requested), 50):
        qids = requested[start : start + 50]
        payload = _api_json(
            endpoint,
            {
                "action": "wbgetentities",
                "format": "json",
                "formatversion": 2,
                "ids": "|".join(qids),
                "props": "sitelinks",
                "maxlag": 5,
            },
        )
        for qid in qids:
            entity = payload.get("entities", {}).get(qid, {})
            for site, item in (entity.get("sitelinks") or {}).items():
                url = str(item.get("url", ""))
                title = str(item.get("title", ""))
                if site == "enwiki" or not title or ".wikipedia.org/wiki/" not in url:
                    continue
                parsed = urllib.parse.urlparse(url)
                language = parsed.hostname.split(".")[0] if parsed.hostname else ""
                if not language:
                    continue
                sitelinks[qid].append(
                    {
                        "candidate_id": site,
                        "site": site,
                        "language": language,
                        "title": title,
                        "article_url": url,
                        "endpoint": f"{parsed.scheme or 'https'}://{parsed.netloc}/w/api.php",
                    }
                )

    groups: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for qid, items in sitelinks.items():
        for item in items:
            groups.setdefault(item["endpoint"], []).append((qid, item))
    fetched = 0
    total_bytes = 0
    index: dict[str, list[dict[str, Any]]] = {
        qid: list(items) for qid, items in existing_index.items()
    }
    index.update({qid: [] for qid in requested})
    alternate_cache = cache_root / "article-alternates"
    for api_endpoint, entries in sorted(groups.items()):
        for start in range(0, len(entries), batch_limit):
            current = entries[start : start + batch_limit]
            titles = [item["title"] for _, item in current]
            payload = _api_json(
                api_endpoint,
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": 2,
                    "redirects": 1,
                    "converttitles": 1,
                    "prop": "revisions",
                    "rvprop": "ids|size|content",
                    "rvslots": "main",
                    "titles": "|".join(titles),
                    "maxlag": 5,
                },
            )
            pages = parse_pages(payload, titles)
            for (qid, item), title in zip(current, titles):
                page = pages[title]
                revision = (page.get("revisions") or [{}])[0]
                raw = ((revision.get("slots") or {}).get("main") or {}).get("content")
                if raw is None:
                    raw = revision.get("content")
                if not raw or not revision.get("revid"):
                    raise BatchError(f"Missing alternate article content for {qid} {item['site']}")
                article = {
                    "wikidata_id": qid,
                    "name": selected[qid]["name"],
                    "article_url": item["article_url"],
                    "language": item["language"],
                    "requested_title": title,
                    "resolved_title": page["title"],
                    "revision_id": revision["revid"],
                    "article_bytes": len(raw.encode("utf-8")),
                    "raw_wikitext": raw,
                    "fetched_utc": utc_now(),
                }
                article_path = alternate_cache / qid / f"{item['site']}.json"
                packet_path = run_dir / "alternate-packets" / qid / f"{item['site']}.json"
                atomic_write_json(article_path, article)
                atomic_write_json(packet_path, build_packet(article))
                index[qid].append(
                    {
                        "candidate_id": item["site"],
                        "language": item["language"],
                        "article_url": item["article_url"],
                        "resolved_title": page["title"],
                        "revision_id": revision["revid"],
                        "article_bytes": article["article_bytes"],
                        "article_cache": str(article_path),
                        "packet": str(packet_path.relative_to(run_dir)),
                    }
                )
                fetched += 1
                total_bytes += article["article_bytes"]
    for qid in index:
        index[qid].sort(key=lambda item: item["candidate_id"])
    atomic_write_json(index_path, index)
    stats = {
        "requested_people": len(requested),
        "alternate_articles": fetched,
        "article_bytes": total_bytes,
    }
    atomic_write_json(run_dir / "alternate_fetch_stats.json", stats)
    cohort["stages"]["alternate_article_retrieval_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    return stats


def init_proposals(*, cohort_value: Path, output: Path | None = None) -> Path:
    run_dir, cohort = cohort_paths(cohort_value)
    if output is None:
        output = run_dir / "proposals.jsonl"
    records = []
    for person in cohort["selected"]:
        records.append(
            {
                "wikidata_id": person["wikidata_id"],
                "cause": [] if person["needs"]["cause"] else None,
                "manner": [] if person["needs"]["manner"] else None,
                "occupation": [] if person["needs"]["occupation"] else None,
                "status": "",
                "article_eligibility": _article_review_template(),
                "evidence_basis": {
                    field: (
                        {
                            "source_tier": "",
                            "section": "",
                            "excerpt": "",
                            "reason": "",
                        }
                        if person["needs"][field]
                        else None
                    )
                    for field in FIELD_SPECS
                },
                "unknown_review": {"cause": None, "manner": None},
                "source_anomalies": [],
            }
        )
    atomic_write_text(
        output, "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    )
    return output


def load_proposals(path: Path) -> list[dict[str, Any]]:
    proposals = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BatchError(f"Invalid proposal JSON on line {line_number}") from exc
        if not isinstance(value, dict):
            raise BatchError(f"Proposal line {line_number} is not an object")
        proposals.append(value)
    return proposals


def write_proposals(path: Path, proposals: Sequence[Mapping[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in proposals),
    )


def _validate_pairs(qid: str, field: str, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise BatchError(f"{qid}: required {field} proposal is empty")
    pairs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "qid"}:
            raise BatchError(f"{qid}: malformed {field} pair")
        label = str(item["label"]).strip()
        item_qid = str(item["qid"]).strip()
        if not label:
            raise BatchError(f"{qid}: blank {field} label")
        pairs.append({"label": label, "qid": item_qid})
    if [pair["label"] for pair in pairs] != sorted(
        [pair["label"] for pair in pairs], key=str.casefold
    ):
        raise BatchError(f"{qid}: unsorted {field} labels")
    if len({pair["label"].casefold() for pair in pairs}) != len(pairs):
        raise BatchError(f"{qid}: duplicate {field} labels")
    if any(pair["label"] == "somevalue" for pair in pairs):
        if pairs != [{"label": "somevalue", "qid": ""}]:
            raise BatchError(f"{qid}: somevalue must be the sole {field} value")
        return pairs
    for pair in pairs:
        if not QID_RE.fullmatch(pair["qid"]):
            raise BatchError(f"{qid}: missing/invalid QID for {field} {pair['label']!r}")
    return pairs


def _evidence_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = []
        for key, text in value.items():
            if isinstance(text, dict):
                detail = "; ".join(
                    f"{nested_key}={str(nested_value).strip()}"
                    for nested_key, nested_value in text.items()
                    if str(nested_value).strip()
                )
            else:
                detail = str(text).strip()
            if detail:
                parts.append(f"{key}: {detail}")
        return " | ".join(parts)
    return ""


def _proposal_evidence_text(proposal: Mapping[str, Any]) -> str:
    parts = [_evidence_text(proposal.get("evidence_basis"))]
    eligibility = proposal.get("article_eligibility")
    if isinstance(eligibility, dict):
        reasons = eligibility.get("removal_reasons")
        if isinstance(reasons, list) and reasons:
            parts.append("possible removal: " + ", ".join(str(item) for item in reasons))
        primary = eligibility.get("primary_review")
        if isinstance(primary, dict) and str(primary.get("reason", "")).strip():
            parts.append("English article review: " + str(primary["reason"]).strip())
        alternates = eligibility.get("alternate_reviews")
        if isinstance(alternates, dict):
            for candidate_id, review in sorted(alternates.items()):
                if isinstance(review, dict) and str(review.get("reason", "")).strip():
                    parts.append(
                        f"{candidate_id} article review: {str(review['reason']).strip()}"
                    )
    unknown_review = proposal.get("unknown_review")
    if isinstance(unknown_review, dict):
        for field in ("cause", "manner"):
            review = unknown_review.get(field)
            if isinstance(review, dict) and str(review.get("conclusion", "")).strip():
                parts.append(
                    f"{field} unknown audit: {str(review['conclusion']).strip()}"
                )
    anomalies = proposal.get("source_anomalies")
    if isinstance(anomalies, list) and anomalies:
        parts.append("source anomalies: " + "; ".join(str(item) for item in anomalies))
    return " | ".join(part for part in parts if part)


def _packet_source_text(
    packet: Mapping[str, Any], source_tier: str, section: str
) -> str:
    if source_tier in {
        "lead_sentence",
        "rest_of_lead_paragraph",
        "remaining_lead_section",
        "infobox",
    }:
        return str(packet.get(source_tier, ""))
    if source_tier == "rest_of_article":
        if section:
            for item in packet.get("article_sections", []):
                if str(item.get("heading", "")).casefold() == section.casefold():
                    return str(item.get("text", ""))
        return str(packet.get("rest_of_article", ""))
    return ""


def _validate_field_evidence(
    *,
    qid: str,
    field: str,
    value: object,
    pairs: Sequence[Mapping[str, str]],
    packet: Mapping[str, Any],
    available_evidence_ids: set[str] | None = None,
) -> None:
    if isinstance(value, dict) and set(value) == {"evidence_ids", "reason"}:
        evidence_ids = value["evidence_ids"]
        if (
            not isinstance(evidence_ids, list)
            or any(not isinstance(item, str) for item in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise BatchError(f"{qid}: malformed {field} evidence IDs")
        if available_evidence_ids is None or not set(evidence_ids).issubset(
            available_evidence_ids
        ):
            raise BatchError(f"{qid}: unknown {field} evidence ID")
        if not str(value["reason"]).strip():
            raise BatchError(f"{qid}: blank {field} evidence reason")
        return
    required = {"source_tier", "section", "excerpt", "reason"}
    if not isinstance(value, dict) or set(value) != required:
        raise BatchError(f"{qid}: malformed {field} evidence basis")
    source_tier = str(value["source_tier"]).strip()
    section = str(value["section"]).strip()
    excerpt = str(value["excerpt"]).strip()
    reason = str(value["reason"]).strip()
    if source_tier not in SOURCE_TIERS:
        raise BatchError(f"{qid}: invalid {field} evidence source tier")
    if not reason:
        raise BatchError(f"{qid}: blank {field} evidence reason")
    is_unknown = pairs == [{"label": "somevalue", "qid": ""}]
    if not is_unknown and (source_tier == "none" or not excerpt):
        raise BatchError(f"{qid}: concrete {field} needs a source excerpt")
    if source_tier == "none":
        if section or excerpt:
            raise BatchError(f"{qid}: none-tier {field} evidence cannot cite text")
        return
    if not excerpt:
        raise BatchError(f"{qid}: cited {field} evidence excerpt is blank")
    source_text = _packet_source_text(packet, source_tier, section)
    if not source_text:
        raise BatchError(f"{qid}: {field} evidence source does not exist")
    if re.sub(r"\s+", " ", excerpt).casefold() not in re.sub(
        r"\s+", " ", source_text
    ).casefold():
        raise BatchError(f"{qid}: {field} evidence excerpt is not in its packet source")


def _validate_unknown_review(
    *, qid: str, field: str, value: object, packet: Mapping[str, Any]
) -> None:
    required = {"reviewed_source_tiers", "candidate_dispositions", "conclusion"}
    if not isinstance(value, dict) or set(value) != required:
        raise BatchError(f"{qid}: malformed {field} unknown review")
    if value["reviewed_source_tiers"] != DEATH_REVIEW_TIERS:
        raise BatchError(f"{qid}: incomplete {field} unknown source-tier review")
    conclusion = str(value["conclusion"]).strip()
    if not conclusion:
        raise BatchError(f"{qid}: blank {field} unknown conclusion")
    dispositions = value["candidate_dispositions"]
    if not isinstance(dispositions, dict):
        raise BatchError(f"{qid}: malformed {field} candidate dispositions")
    candidates = {
        str(item.get("candidate_id", "")): item
        for item in packet.get("death_evidence_candidates", [])
    }
    if set(dispositions) != set(candidates):
        missing = sorted(set(candidates) - set(dispositions))
        extra = sorted(set(dispositions) - set(candidates))
        raise BatchError(
            f"{qid}: {field} unknown candidate audit mismatch; "
            f"missing={missing}, extra={extra}"
        )
    for candidate_id, disposition in dispositions.items():
        if not isinstance(disposition, dict) or set(disposition) != {
            "disposition",
            "reason",
        }:
            raise BatchError(
                f"{qid}: malformed {field} disposition for {candidate_id}"
            )
        if disposition["disposition"] not in UNKNOWN_DISPOSITIONS:
            raise BatchError(
                f"{qid}: invalid {field} disposition for {candidate_id}"
            )
        if not str(disposition["reason"]).strip():
            raise BatchError(f"{qid}: blank {field} reason for {candidate_id}")


def _validate_page_review(qid: str, candidate_id: str, value: object) -> dict[str, str]:
    legacy_required = {
        "page_kind",
        "subject_is_human",
        "life_status",
        "age_compatibility",
        "reason",
    }
    if not isinstance(value, dict) or set(value) not in {
        frozenset(legacy_required),
        frozenset(legacy_required | {"subject_match"}),
    }:
        raise BatchError(f"{qid}: malformed article review for {candidate_id}")
    review = {key: str(value[key]).strip() for key in legacy_required}
    review["subject_match"] = str(value.get("subject_match", "match")).strip()
    if review["page_kind"] not in PAGE_KINDS:
        raise BatchError(f"{qid}: invalid page kind for {candidate_id}")
    if review["subject_is_human"] not in HUMAN_STATUSES:
        raise BatchError(f"{qid}: invalid human status for {candidate_id}")
    if review["life_status"] not in LIFE_STATUSES:
        raise BatchError(f"{qid}: invalid life status for {candidate_id}")
    if review["age_compatibility"] not in AGE_COMPATIBILITIES:
        raise BatchError(f"{qid}: invalid age compatibility for {candidate_id}")
    if review["subject_match"] not in SUBJECT_MATCHES:
        raise BatchError(f"{qid}: invalid subject match for {candidate_id}")
    if not review["reason"]:
        raise BatchError(f"{qid}: blank article-review reason for {candidate_id}")
    return review


def _validate_article_eligibility(
    *, qid: str, value: object, run_dir: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    required = {
        "decision",
        "selected_article",
        "primary_review",
        "alternate_reviews",
        "removal_reasons",
    }
    allowed_keys = {frozenset(required), frozenset(required | {"stay_overrides"})}
    if not isinstance(value, dict) or frozenset(value) not in allowed_keys:
        raise BatchError(f"{qid}: malformed article eligibility review")
    decision = str(value["decision"]).strip()
    if decision not in {"eligible", "possible_removal"}:
        raise BatchError(f"{qid}: invalid article eligibility decision")
    primary_review = _validate_page_review(qid, "enwiki", value["primary_review"])
    alternate_index_path = run_dir / "alternate_article_index.json"
    alternate_index = (
        json.loads(alternate_index_path.read_text(encoding="utf-8"))
        if alternate_index_path.exists()
        else {}
    )
    candidate_items = {
        str(item["candidate_id"]): item for item in alternate_index.get(qid, [])
    }
    alternate_reviews = value["alternate_reviews"]
    if not isinstance(alternate_reviews, dict):
        raise BatchError(f"{qid}: malformed alternate article reviews")
    if primary_review["page_kind"] != "person":
        if qid not in alternate_index:
            raise BatchError(f"{qid}: non-person English page requires alternate fetch")
        if set(alternate_reviews) != set(candidate_items):
            raise BatchError(f"{qid}: incomplete alternate article review")
    elif alternate_reviews:
        raise BatchError(f"{qid}: qualifying English page cannot have alternate reviews")
    reviewed = {"enwiki": primary_review}
    for candidate_id, review in alternate_reviews.items():
        if candidate_id not in candidate_items:
            raise BatchError(f"{qid}: unknown alternate article {candidate_id}")
        reviewed[candidate_id] = _validate_page_review(qid, candidate_id, review)

    removal_reasons = value["removal_reasons"]
    if (
        not isinstance(removal_reasons, list)
        or len(removal_reasons) != len(set(removal_reasons))
        or any(reason not in REMOVAL_REASONS for reason in removal_reasons)
    ):
        raise BatchError(f"{qid}: invalid removal reasons")
    stay_overrides = value.get("stay_overrides", [])
    if (
        not isinstance(stay_overrides, list)
        or len(stay_overrides) != len(set(stay_overrides))
        or any(override not in STAY_OVERRIDES for override in stay_overrides)
    ):
        raise BatchError(f"{qid}: invalid stay overrides")
    stay_overrides = sorted(stay_overrides)
    selected_article = str(value["selected_article"]).strip()
    if decision == "eligible":
        if removal_reasons:
            raise BatchError(f"{qid}: eligible article cannot have removal reasons")
        if selected_article not in reviewed:
            raise BatchError(f"{qid}: selected article was not reviewed")
        chosen = reviewed[selected_article]
        if chosen["page_kind"] != "person":
            if not stay_overrides:
                raise BatchError(f"{qid}: selected article is not a human-person page")
            if _deterministic_removal_reasons(reviewed) != [
                "no_dedicated_person_article"
            ]:
                raise BatchError(f"{qid}: stay override does not replace the removal reasons")
        # The approved-musician/archived-27-club stay is deliberately allowed
        # to retain a named member whose English sitelink redirects to the
        # group article.  The redirect remains visible in the review; the
        # override is the explicit provenance for treating it as eligible.
        stay_subject_override = (
            set(stay_overrides)
            == {"approved_musician_occupation", "archived_27_club_article"}
            and qid in _load_approved_musician_qids()
        )
        if (
            (chosen["subject_match"] != "match" or chosen["subject_is_human"] == "nonhuman")
            and not stay_subject_override
        ):
            raise BatchError(f"{qid}: selected article is not a matching human subject")
        if chosen["life_status"] in {"living", "conflicting"}:
            raise BatchError(f"{qid}: selected article has a living-status conflict")
        if chosen["age_compatibility"] in {"outside_26_28", "conflicting"}:
            raise BatchError(f"{qid}: selected article has an age conflict")
        if selected_article != "enwiki":
            qualifying = [
                candidate_id
                for candidate_id, review in reviewed.items()
                if candidate_id != "enwiki"
                and review["page_kind"] == "person"
                and review["subject_match"] == "match"
                and review["subject_is_human"] != "nonhuman"
                and review["life_status"] not in {"living", "conflicting"}
                and review["age_compatibility"]
                not in {"outside_26_28", "conflicting"}
            ]
            largest = min(
                qualifying,
                key=lambda candidate_id: (
                    -int(candidate_items[candidate_id]["article_bytes"]), candidate_id
                ),
            )
            if selected_article != largest:
                raise BatchError(f"{qid}: selected alternate is not the largest qualifying article")
        if stay_overrides:
            candidates_for_override = {
                str(item["candidate_id"]): item
                for item in alternate_index.get(qid, [])
            }
            supported: list[str] = []
            if qid in _load_approved_musician_qids():
                supported.append("approved_musician_occupation")
            if selected_article == "enwiki":
                packet_for_override = _candidate_packet(
                    run_dir, qid, selected_article, candidates_for_override
                )
                if _normalize_enwiki_article_url(
                    packet_for_override.get("article_url")
                ) in _load_archived_27_club_urls():
                    supported.append("archived_27_club_article")
            if _deterministic_removal_reasons(reviewed) != [
                "no_dedicated_person_article"
            ] or set(stay_overrides) != set(supported):
                raise BatchError(f"{qid}: unsupported or incomplete stay override")
    else:
        if stay_overrides:
            raise BatchError(f"{qid}: possible removal cannot have stay overrides")
        if not removal_reasons:
            raise BatchError(f"{qid}: possible removal needs at least one reason")
        if selected_article and selected_article not in reviewed:
            raise BatchError(f"{qid}: removal selection was not reviewed")
        if "no_dedicated_person_article" in removal_reasons and any(
            review["page_kind"] == "person" for review in reviewed.values()
        ):
            raise BatchError(f"{qid}: dedicated person article contradicts removal reason")
        chosen = reviewed.get(selected_article, primary_review)
        reason_checks = {
            "living": chosen["life_status"] in {"living", "conflicting"},
            "nonhuman": chosen["subject_is_human"] == "nonhuman",
            "age_outside_26_28": chosen["age_compatibility"]
            in {"outside_26_28", "conflicting"},
            "no_dedicated_person_article": not any(
                review["page_kind"] == "person" for review in reviewed.values()
            ),
            "subject_identity_mismatch": True,
        }
        for reason in removal_reasons:
            if not reason_checks[reason]:
                raise BatchError(f"{qid}: article review does not support {reason}")

    if selected_article == "enwiki" or not selected_article:
        packet_path = run_dir / "packets" / f"{qid}.json"
        article_kind = {"candidate_id": "enwiki"}
    else:
        item = candidate_items[selected_article]
        packet_path = run_dir / str(item["packet"])
        article_kind = item
    if not packet_path.exists():
        raise BatchError(f"{qid}: selected article packet is missing")
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if packet.get("schema_version") != 3:
        raise BatchError(f"{qid}: unsupported selected article packet schema")
    return dict(value), packet, article_kind


def verify_protected(
    cohort: Mapping[str, Any],
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    removed_rows: Mapping[str, Mapping[str, str]] | None = None,
) -> None:
    if list(fieldnames) != list(cohort["public_csv_columns"]):
        raise BatchError("Public people CSV schema changed after cohort selection")
    by_qid = {row["wikidata_id"]: row for row in rows}
    removed_rows = removed_rows or {}
    current = {}
    for qid, snapshot in cohort["protected_rows"].items():
        source = by_qid.get(qid) or removed_rows.get(qid)
        if source is None:
            raise BatchError(f"Selected QID disappeared: {qid}")
        current[qid] = {
            column: source.get(column, "")
            for column in cohort["protected_columns"]
        }
        if current[qid] != snapshot:
            raise BatchError(f"Protected columns changed for {qid}")
    digest = hashlib.sha256(canonical_json(current).encode()).hexdigest()
    if digest != cohort["protected_snapshot_sha256"]:
        raise BatchError("Protected cohort snapshot hash changed")


def validate_proposals(
    *,
    cohort_value: Path,
    proposals_path: Path,
    people_csv: Path,
    cache_root: Path,
    expected_qids: Sequence[str] | None = None,
    allow_extra_proposals: bool = False,
) -> dict[str, Any]:
    run_dir, cohort = cohort_paths(cohort_value)
    fieldnames, rows = read_csv(people_csv)
    verify_protected(cohort, fieldnames, rows)
    rows_by_qid = {row["wikidata_id"]: row for row in rows}
    proposals = load_proposals(proposals_path)
    selected_all = {person["wikidata_id"]: person for person in cohort["selected"]}
    selected = (
        selected_all
        if expected_qids is None
        else {qid: selected_all[qid] for qid in expected_qids if qid in selected_all}
    )
    if expected_qids is not None and set(selected) != set(expected_qids):
        raise BatchError("Expected proposal QIDs are outside the frozen cohort")
    proposal_qids = [str(proposal.get("wikidata_id", "")) for proposal in proposals]
    if len(proposal_qids) != len(set(proposal_qids)):
        raise BatchError("Duplicate proposal QID")
    if allow_extra_proposals and expected_qids is not None:
        proposals = [
            proposal
            for proposal in proposals
            if str(proposal.get("wikidata_id", "")) in selected
        ]
        proposal_qids = [str(proposal["wikidata_id"]) for proposal in proposals]
    if set(proposal_qids) != set(selected):
        missing = sorted(set(selected) - set(proposal_qids))
        extra = sorted(set(proposal_qids) - set(selected))
        raise BatchError(f"Proposal cohort mismatch; missing={missing}, extra={extra}")
    status_counts = {status: 0 for status in sorted(STATUSES)}
    somevalue_counts = {field: 0 for field in FIELD_SPECS}
    audited_unknown_fields = 0
    for proposal in proposals:
        qid = proposal["wikidata_id"]
        person = selected[qid]
        artifact_proposal = proposal.get("proposal_schema_version") == 2
        if proposal.get("status") not in STATUSES:
            raise BatchError(f"{qid}: invalid/blank status")
        if artifact_proposal and proposal["status"] not in {
            "settled",
            "unknown",
            "possible_removal",
        }:
            raise BatchError(f"{qid}: invalid semantic death status")
        status_counts[proposal["status"]] += 1
        evidence = proposal.get("evidence_basis")
        if not isinstance(evidence, dict) or set(evidence) != set(FIELD_SPECS):
            raise BatchError(f"{qid}: malformed field-specific evidence basis")
        unknown_review = proposal.get("unknown_review")
        if artifact_proposal:
            if unknown_review is not None:
                raise BatchError(f"{qid}: artifact proposal cannot contain unknown_review")
            unknown_review = {"cause": None, "manner": None}
        elif not isinstance(unknown_review, dict) or set(unknown_review) != {
            "cause", "manner"
        }:
            raise BatchError(f"{qid}: malformed unknown review container")
        anomalies = proposal.get("source_anomalies")
        if not isinstance(anomalies, list) or any(
            not isinstance(item, str) or not item.strip() for item in anomalies
        ):
            raise BatchError(f"{qid}: source anomalies must be nonblank strings")
        article_path = cache_root / "articles" / f"{qid}.json"
        if not article_path.exists():
            raise BatchError(f"{qid}: missing article cache")
        article = json.loads(article_path.read_text(encoding="utf-8"))
        for key in (
            "wikidata_id",
            "article_url",
            "language",
            "resolved_title",
            "revision_id",
            "article_bytes",
            "raw_wikitext",
        ):
            if article.get(key) in ("", None):
                raise BatchError(f"{qid}: article cache missing {key}")
        if re.match(r"^\s*#redirect\b", article["raw_wikitext"], flags=re.I):
            raise BatchError(f"{qid}: unresolved redirect in article cache")
        article_eligibility, packet, _ = _validate_article_eligibility(
            qid=qid,
            value=proposal.get("article_eligibility"),
            run_dir=run_dir,
        )
        possible_removal = article_eligibility["decision"] == "possible_removal"
        if possible_removal != (proposal["status"] == "possible_removal"):
            raise BatchError(f"{qid}: article decision/status mismatch")
        semantic_evidence: dict[str, Any] | None = None
        available_evidence_ids: set[str] | None = None
        if artifact_proposal and not possible_removal:
            semantic_evidence = _validate_death_evidence(run_dir, qid)
            available_evidence_ids = {
                item["evidence_id"] for item in semantic_evidence["evidence"]
            }
        parsed_pairs: dict[str, list[dict[str, str]]] = {}
        for field in FIELD_SPECS:
            value = proposal.get(field)
            if possible_removal:
                if value is not None or evidence[field] is not None:
                    raise BatchError(
                        f"{qid}: possible removal must not propose new {field} fallback"
                    )
                if field in {"cause", "manner"} and unknown_review[field] is not None:
                    raise BatchError(
                        f"{qid}: possible removal cannot have a {field} unknown review"
                    )
                continue
            if person["needs"][field]:
                pairs = _validate_pairs(qid, field, value)
                parsed_pairs[field] = pairs
                _validate_field_evidence(
                    qid=qid,
                    field=field,
                    value=evidence[field],
                    pairs=pairs,
                    packet=packet,
                    available_evidence_ids=available_evidence_ids,
                )
                if pairs[0]["label"] == "somevalue":
                    somevalue_counts[field] += 1
                    if field in {"cause", "manner"}:
                        if artifact_proposal:
                            if not semantic_evidence or not semantic_evidence[
                                "no_usable_account"
                            ][field]:
                                raise BatchError(
                                    f"{qid}: somevalue {field} requires no usable account"
                                )
                        else:
                            _validate_unknown_review(
                                qid=qid,
                                field=field,
                                value=unknown_review[field],
                                packet=packet,
                            )
                        audited_unknown_fields += 1
                elif field in {"cause", "manner"} and unknown_review[field] is not None:
                    raise BatchError(
                        f"{qid}: concrete {field} cannot have an unknown review"
                    )
            elif value is not None:
                raise BatchError(
                    f"{qid}: {field} is already effective and proposal must be null"
                )
            elif evidence[field] is not None:
                raise BatchError(
                    f"{qid}: {field} is already effective and evidence must be null"
                )
            elif field in {"cause", "manner"} and unknown_review[field] is not None:
                raise BatchError(
                    f"{qid}: already-effective {field} cannot have an unknown review"
                )

        if possible_removal:
            continue

        row = rows_by_qid[qid]

        def effective_unknown(field: str) -> bool:
            base, fallback, _ = FIELD_SPECS[field]
            if usable_wikidata(row.get(base, "")):
                return False
            if person["needs"][field]:
                return parsed_pairs[field][0]["label"] == "somevalue"
            return "somevalue" in {
                value.casefold() for value in split_values(row.get(fallback, ""))
            }

        death_unknown = effective_unknown("cause") and effective_unknown("manner")
        if artifact_proposal and (proposal["status"] == "unknown") != death_unknown:
            raise BatchError(
                f"{qid}: unknown status must exactly match no usable death account"
            )
    return {
        "selected": len(selected),
        "validated": len(proposals),
        "status_counts": status_counts,
        "somevalue_counts": somevalue_counts,
        "audited_unknown_fields": audited_unknown_fields,
    }


def normalize_vocabulary_label(label: str) -> str:
    return " ".join(label.casefold().split())


def _load_field_vocabulary(path: Path, *, required: bool) -> dict[str, dict[str, dict[str, str]]]:
    mappings: dict[str, dict[str, dict[str, str]]] = {
        field: {} for field in FIELD_SPECS
    }
    if not path.exists():
        if required:
            raise BatchError(f"Trusted vocabulary is missing: {path}")
        return mappings
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("mappings") if isinstance(payload, dict) else None
    if payload.get("schema_version") != 1 or not isinstance(raw, dict):
        raise BatchError(f"Unsupported field-specific vocabulary schema: {path}")
    if set(raw) != set(FIELD_SPECS):
        raise BatchError(f"Vocabulary must be field-specific: {path}")
    for field in FIELD_SPECS:
        values = raw[field]
        if not isinstance(values, dict):
            raise BatchError(f"Malformed {field} vocabulary: {path}")
        for stored_key, value in values.items():
            if not isinstance(value, dict) or set(value) != {"label", "qid"}:
                raise BatchError(f"Malformed {field} vocabulary entry: {stored_key}")
            label = str(value["label"]).strip()
            qid = str(value["qid"]).strip()
            key = normalize_vocabulary_label(str(stored_key))
            if key != stored_key or not label or not key or not QID_RE.fullmatch(qid):
                raise BatchError(f"Invalid {field} vocabulary entry: {stored_key}")
            mappings[field][key] = {"label": label, "qid": qid}
    return mappings


def _seed_vocabulary(
    trusted_path: Path, approved_path: Path
) -> dict[str, dict[str, dict[str, str]]]:
    trusted = _load_field_vocabulary(trusted_path, required=True)
    approved = _load_field_vocabulary(approved_path, required=False)
    for field in FIELD_SPECS:
        for key, value in approved[field].items():
            existing = trusted[field].get(key)
            if existing and existing["qid"] != value["qid"]:
                raise BatchError(
                    f"Conflicting {field} vocabulary mapping for {value['label']!r}: "
                    f"{existing['qid']} vs {value['qid']}"
                )
            trusted[field][key] = value
    return trusted


def resolve_known_vocabulary(
    *,
    proposals_path: Path,
    people_csv: Path,
    vocabulary_path: Path,
    trusted_vocabulary_path: Path = TRUSTED_VOCABULARY,
) -> list[dict[str, str]]:
    proposals = load_proposals(proposals_path)
    del people_csv  # Public/defective cohort rows are never trusted as vocabulary.
    mappings = _seed_vocabulary(trusted_vocabulary_path, vocabulary_path)
    unresolved: dict[tuple[str, str], dict[str, str]] = {}
    for proposal in proposals:
        for field in FIELD_SPECS:
            value = proposal.get(field)
            if not isinstance(value, list):
                continue
            for pair in value:
                label = str(pair.get("label", "")).strip()
                if label == "somevalue":
                    pair["qid"] = ""
                    continue
                key = normalize_vocabulary_label(label)
                known = mappings[field].get(key)
                supplied_qid = str(pair.get("qid", "")).strip()
                if known:
                    if supplied_qid and supplied_qid != known["qid"]:
                        raise BatchError(
                            f"Conflicting {field} QID for {label!r}: "
                            f"{supplied_qid} vs {known['qid']}"
                        )
                    pair["label"] = known["label"]
                    pair["qid"] = known["qid"]
                else:
                    pair["qid"] = ""
                    unresolved[(field, key)] = {
                        "field": field,
                        "label": label,
                        "normalized_label": key,
                    }
            value.sort(key=lambda pair: str(pair.get("label", "")).casefold())
    write_proposals(proposals_path, proposals)
    unresolved_values = [unresolved[key] for key in sorted(unresolved)]
    atomic_write_json(
        proposals_path.with_name("unresolved_vocabulary.json"), unresolved_values
    )
    return unresolved_values


def lookup_vocabulary_candidates(
    *,
    proposals_path: Path,
    cache_root: Path,
    max_cache_age_hours: float = 168,
    limit: int = 8,
) -> dict[str, int]:
    """Cache Wikidata Search API candidates without making semantic choices."""
    if not 1 <= limit <= 50:
        raise BatchError("Vocabulary candidate limit must be between 1 and 50")
    unresolved_path = proposals_path.with_name("unresolved_vocabulary.json")
    if not unresolved_path.exists():
        raise BatchError("Run resolve-known before lookup-vocabulary")
    unresolved = json.loads(unresolved_path.read_text(encoding="utf-8"))
    if not isinstance(unresolved, list) or any(
        not isinstance(item, dict)
        or set(item) != {"field", "label", "normalized_label"}
        or item["field"] not in FIELD_SPECS
        or normalize_vocabulary_label(str(item["label"])) != item["normalized_label"]
        for item in unresolved
    ):
        raise BatchError("Invalid unresolved_vocabulary.json")
    labels = sorted(
        {str(item["label"]).strip() for item in unresolved}, key=str.casefold
    )
    search_root = cache_root / "vocabulary-search"
    search_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    endpoint = "https://www.wikidata.org/w/api.php"
    results: dict[str, list[dict[str, str]]] = {}
    cache_hits = 0
    lookups = 0
    for label in labels:
        cache_key = hashlib.sha256(label.casefold().encode()).hexdigest()
        cache_path = search_root / f"{cache_key}.json"
        cached: dict[str, Any] | None = None
        if cache_path.exists():
            candidate = json.loads(cache_path.read_text(encoding="utf-8"))
            try:
                fetched = datetime.fromisoformat(str(candidate["fetched_utc"]))
            except (KeyError, TypeError, ValueError):
                fetched = datetime.min.replace(tzinfo=timezone.utc)
            age_hours = (now - fetched.astimezone(timezone.utc)).total_seconds() / 3600
            if (
                candidate.get("label", "").casefold() == label.casefold()
                and isinstance(candidate.get("candidates"), list)
                and age_hours <= max_cache_age_hours
            ):
                cached = candidate
        if cached is not None:
            cache_hits += 1
            results[label] = cached["candidates"]
            continue
        payload = _api_json(
            endpoint,
            {
                "action": "wbsearchentities",
                "format": "json",
                "formatversion": 2,
                "language": "en",
                "uselang": "en",
                "type": "item",
                "limit": limit,
                "search": label,
                "maxlag": 5,
            },
            timeout=30,
        )
        candidates = []
        for item in payload.get("search", []):
            qid = str(item.get("id", ""))
            if not QID_RE.fullmatch(qid):
                continue
            candidates.append(
                {
                    "id": qid,
                    "label": str(item.get("label", "")),
                    "description": str(item.get("description", "")),
                }
            )
        atomic_write_json(
            cache_path,
            {"label": label, "fetched_utc": utc_now(), "candidates": candidates},
        )
        results[label] = candidates
        lookups += 1
    output_records = []
    for item in unresolved:
        output_records.append(
            {
                **item,
                "candidates": results[str(item["label"]).strip()],
            }
        )
    output = {
        "schema_version": 1,
        "generated_utc": utc_now(),
        "items": output_records,
    }
    atomic_write_json(proposals_path.with_name("vocabulary_candidates.json"), output)
    return {
        "unique_labels": len(labels),
        "cache_hits": cache_hits,
        "external_lookups": lookups,
    }


def vocabulary_artifact_path(run_dir: Path, field: str, label: str) -> Path:
    if field not in FIELD_SPECS:
        raise BatchError(f"Unknown vocabulary field: {field}")
    normalized = normalize_vocabulary_label(label)
    digest = hashlib.sha256(f"{field}\0{normalized}".encode()).hexdigest()[:16]
    return run_dir / "semantic" / "vocabulary" / f"{field}-{digest}.json"


def build_vocabulary_input(
    *, proposals_path: Path, field: str, label: str, output: Path | None = None
) -> Path:
    candidate_path = proposals_path.with_name("vocabulary_candidates.json")
    if not candidate_path.exists():
        raise BatchError("Run lookup-vocabulary before building vocabulary input")
    normalized = normalize_vocabulary_label(label)
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    matches = [
        item
        for item in payload.get("items", [])
        if item.get("field") == field and item.get("normalized_label") == normalized
    ]
    if len(matches) != 1:
        raise BatchError(f"Expected one vocabulary candidate item for {field}:{label}")
    if output is None:
        digest = hashlib.sha256(f"{field}\0{normalized}".encode()).hexdigest()[:16]
        output = proposals_path.parent / "agent-inputs" / "vocabulary" / f"{field}-{digest}.json"
    atomic_write_json(output, {"schema_version": 1, "role": "vocabulary", **matches[0]})
    return output


def record_vocabulary_artifact(
    *, cohort_value: Path, input_path: Path, _scheduler_install: bool = False
) -> Path:
    run_dir, cohort = cohort_paths(cohort_value)
    if cohort.get("selection_mode") == "all_eligible" and not _scheduler_install:
        raise BatchError(
            "All-eligible queues install vocabulary only through complete-assignment"
        )
    value = json.loads(input_path.read_text(encoding="utf-8"))
    required = {"schema_version", "field", "label", "decision", "selected_qid", "reason"}
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1:
        raise BatchError("Malformed vocabulary artifact")
    field, label = str(value["field"]), str(value["label"]).strip()
    if field not in FIELD_SPECS or not label or value["decision"] not in {"approved", "no_adequate_candidate"}:
        raise BatchError("Invalid vocabulary artifact decision")
    selected_qid = str(value["selected_qid"]).strip()
    if (value["decision"] == "approved") != bool(QID_RE.fullmatch(selected_qid)):
        raise BatchError("Vocabulary approval must select one QID")
    if not str(value["reason"]).strip():
        raise BatchError("Vocabulary artifact reason is blank")
    destination = vocabulary_artifact_path(run_dir, field, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise BatchError(f"Vocabulary artifact already exists: {destination}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return destination


def apply_vocabulary_artifacts(
    *, cohort_value: Path, proposals_path: Path, vocabulary_path: Path
) -> dict[str, int]:
    run_dir, _ = cohort_paths(cohort_value)
    unresolved_path = proposals_path.with_name("unresolved_vocabulary.json")
    candidate_path = proposals_path.with_name("vocabulary_candidates.json")
    if not unresolved_path.exists() or not candidate_path.exists():
        raise BatchError("Run resolve-known and lookup-vocabulary first")
    unresolved = json.loads(unresolved_path.read_text(encoding="utf-8"))
    candidates_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate_items = {
        (item["field"], item["normalized_label"]): item
        for item in candidates_payload.get("items", [])
    }
    approved = _load_field_vocabulary(vocabulary_path, required=False)
    chosen: dict[tuple[str, str], dict[str, str]] = {}
    for item in unresolved:
        field, label, normalized = item["field"], item["label"], item["normalized_label"]
        artifact_path = vocabulary_artifact_path(run_dir, field, label)
        if not artifact_path.exists():
            raise BatchError(f"Missing vocabulary artifact for {field}:{label}")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if artifact.get("field") != field or normalize_vocabulary_label(str(artifact.get("label", ""))) != normalized:
            raise BatchError(f"Vocabulary artifact identity mismatch for {field}:{label}")
        if artifact.get("decision") != "approved":
            raise BatchError(f"No adequate QID for {field}:{label}")
        qid = str(artifact.get("selected_qid", ""))
        candidates = candidate_items.get((field, normalized), {}).get("candidates", [])
        selected = next((candidate for candidate in candidates if candidate.get("id") == qid), None)
        if selected is None:
            raise BatchError(f"Selected QID was not a candidate for {field}:{label}")
        canonical = str(selected.get("label") or label).strip()
        chosen[(field, normalized)] = {"label": canonical, "qid": qid}
        mapping = {"label": canonical, "qid": qid}
        existing = approved[field].get(normalized)
        if existing and existing["qid"] != qid:
            raise BatchError(f"Conflicting approved mapping for {field}:{label}")
        # Persist the reviewed input label as the exact dictionary key. The
        # candidate's canonical label may differ and is the value we publish.
        approved[field][normalized] = mapping
    proposals = load_proposals(proposals_path)
    for proposal in proposals:
        for field in FIELD_SPECS:
            for pair in proposal.get(field) or []:
                key = (field, normalize_vocabulary_label(str(pair.get("label", ""))))
                if key in chosen:
                    pair.update(chosen[key])
    write_proposals(proposals_path, proposals)
    atomic_write_json(vocabulary_path, {"schema_version": 1, "mappings": approved})
    atomic_write_json(unresolved_path, [])
    return {"resolved": len(chosen)}


def _serialize_pairs(value: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    labels = "; ".join(pair["label"] for pair in value)
    qids = "; ".join(pair["qid"] for pair in value if pair["qid"])
    return labels, qids


def validate_public_rows(
    fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> None:
    if len({row["wikidata_id"] for row in rows}) != len(rows):
        raise BatchError("Duplicate Wikidata ID in people CSV")
    expected = list(fieldnames)
    for row in rows:
        if list(row) != expected:
            raise BatchError("People CSV row schema mismatch")
        for _, label_column, qid_column in FIELD_SPECS.values():
            labels = split_values(row.get(label_column, ""))
            qids = split_values(row.get(qid_column, ""))
            if labels != sorted(labels, key=str.casefold):
                raise BatchError(f"{row['wikidata_id']}: unsorted {label_column}")
            if labels == ["somevalue"]:
                if qids:
                    raise BatchError(f"{row['wikidata_id']}: somevalue has QID")
            elif len(labels) != len(qids):
                raise BatchError(f"{row['wikidata_id']}: unaligned {label_column}")
            elif any(not QID_RE.fullmatch(qid) for qid in qids):
                raise BatchError(f"{row['wikidata_id']}: invalid fallback QID")
        status = row.get("wikipedia_death_review_status", "")
        if status and status not in STATUSES:
            raise BatchError(f"{row['wikidata_id']}: invalid status {status!r}")


def _proposal_review_row(
    proposal: Mapping[str, Any], article: Mapping[str, Any], name: str
) -> dict[str, object]:
    serialized = {}
    for field in FIELD_SPECS:
        value = proposal.get(field)
        serialized[field] = _serialize_pairs(value) if isinstance(value, list) else ("", "")
    return {
        "wikidata_id": proposal["wikidata_id"],
        "name": name,
        "article_url": article["article_url"],
        "language": article["language"],
        "revision_id": article["revision_id"],
        "article_bytes": article["article_bytes"],
        "status": proposal["status"],
        "proposed_cause": serialized["cause"][0],
        "proposed_cause_qid": serialized["cause"][1],
        "proposed_manner": serialized["manner"][0],
        "proposed_manner_qid": serialized["manner"][1],
        "proposed_occupation": serialized["occupation"][0],
        "proposed_occupation_qid": serialized["occupation"][1],
        "evidence_basis": _proposal_evidence_text(proposal),
    }


def _proposal_article(
    proposal: Mapping[str, Any], qid: str, run_dir: Path, cache_root: Path
) -> dict[str, Any]:
    eligibility = proposal.get("article_eligibility", {})
    selected_article = str(eligibility.get("selected_article", ""))
    if not selected_article or selected_article == "enwiki":
        path = cache_root / "articles" / f"{qid}.json"
    else:
        index = json.loads(
            (run_dir / "alternate_article_index.json").read_text(encoding="utf-8")
        )
        matches = [
            item
            for item in index.get(qid, [])
            if item.get("candidate_id") == selected_article
        ]
        if len(matches) != 1:
            raise BatchError(f"{qid}: selected alternate article is unavailable")
        path = Path(str(matches[0]["article_cache"]))
    return json.loads(path.read_text(encoding="utf-8"))


def apply_proposals(
    *,
    cohort_value: Path,
    proposals_path: Path,
    people_csv: Path,
    review_csv: Path,
    cache_root: Path,
    tranche: int | None = None,
) -> dict[str, Any]:
    run_dir, cohort = cohort_paths(cohort_value)
    expected_qids = (
        None
        if tranche is None
        else [
            person["wikidata_id"]
            for person in cohort["selected"]
            if int(person.get("approval_tranche", 1)) == tranche
            and person["wikidata_id"]
            in {proposal["wikidata_id"] for proposal in load_proposals(proposals_path)}
        ]
    )
    result = validate_proposals(
        cohort_value=cohort_value,
        proposals_path=proposals_path,
        people_csv=people_csv,
        cache_root=cache_root,
        expected_qids=expected_qids,
    )
    fieldnames, rows = read_csv(people_csv)
    proposals = {
        proposal["wikidata_id"]: proposal for proposal in load_proposals(proposals_path)
    }
    selected = {
        person["wikidata_id"]: person
        for person in cohort["selected"]
        if expected_qids is None or person["wikidata_id"] in set(expected_qids)
    }
    by_qid = {row["wikidata_id"]: row for row in rows}
    for qid, person in selected.items():
        row = by_qid[qid]
        proposal = proposals[qid]
        possible_removal = proposal["status"] == "possible_removal"
        for field, (_, fallback, qid_column) in FIELD_SPECS.items():
            if person["needs"][field] and not possible_removal:
                labels, qids = _serialize_pairs(proposal[field])
                row[fallback] = labels
                row[qid_column] = qids
        row["wikipedia_death_review_status"] = proposal["status"]
    validate_public_rows(fieldnames, rows)

    existing_review: dict[str, dict[str, str]] = {}
    if review_csv.exists():
        review_fields, review_rows = read_csv(review_csv)
        if review_fields != REVIEW_COLUMNS:
            raise BatchError("Review queue schema mismatch")
        for row in review_rows:
            qid = row["wikidata_id"]
            if qid in existing_review:
                raise BatchError(f"Duplicate review queue QID: {qid}")
            existing_review[qid] = row
    for qid, proposal in proposals.items():
        existing_review.pop(qid, None)
        if proposal["status"] in {
            "provisional",
            "disputed",
            "unknown",
            "possible_removal",
        }:
            article = _proposal_article(proposal, qid, run_dir, cache_root)
            existing_review[qid] = {
                key: str(value)
                for key, value in _proposal_review_row(
                    proposal, article, selected[qid]["name"]
                ).items()
            }
    review_rows_out = [existing_review[qid] for qid in sorted(existing_review)]

    research_rows = []
    for person in selected.values():
        qid = person["wikidata_id"]
        article = _proposal_article(proposals[qid], qid, run_dir, cache_root)
        research_rows.append(
            {
                "wikidata_id": qid,
                "name": person["name"],
                "article_url": article["article_url"],
                "language": article["language"],
                "resolved_title": article["resolved_title"],
                "revision_id": article["revision_id"],
                "article_bytes": article["article_bytes"],
                "proposal": proposals[qid],
            }
        )

    # Both complete outputs are prepared and validated before either replacement.
    atomic_write_csv(people_csv, fieldnames, rows)
    atomic_write_csv(review_csv, REVIEW_COLUMNS, review_rows_out)
    atomic_write_text(
        proposals_path.parent / "research_log.jsonl",
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in research_rows),
    )
    stage_key = (
        "staging_apply_complete_utc"
        if tranche is None
        else f"tranche_{tranche}_staging_apply_complete_utc"
    )
    cohort["stages"][stage_key] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    result["review_queue_rows"] = len(review_rows_out)
    return result


def _review_payload(
    *,
    qids: Sequence[str],
    proposals_path: Path,
    staged_people_csv: Path,
    staged_review_csv: Path,
) -> dict[str, Any]:
    people_fields, people_rows = read_csv(staged_people_csv)
    review_fields, review_rows = read_csv(staged_review_csv)
    if review_fields != REVIEW_COLUMNS:
        raise BatchError("Review queue schema mismatch")
    qid_set = set(qids)
    proposals = [
        proposal
        for proposal in load_proposals(proposals_path)
        if proposal.get("wikidata_id") in qid_set
    ]
    people = [row for row in people_rows if row.get("wikidata_id") in qid_set]
    reviews = [row for row in review_rows if row.get("wikidata_id") in qid_set]
    if {item["wikidata_id"] for item in proposals} != qid_set:
        raise BatchError("Reviewed proposal set does not match approval QIDs")
    if {item["wikidata_id"] for item in people} != qid_set:
        raise BatchError("Reviewed staged rows do not match approval QIDs")
    return {
        "qids": list(qids),
        "people_columns": people_fields,
        "people": people,
        "review_columns": review_fields,
        "reviews": reviews,
        "proposals": proposals,
    }


def _review_item_hashes(payload: Mapping[str, Any]) -> dict[str, str]:
    """Fingerprint each reviewed item from one already-loaded tranche payload."""
    return {
        qid: hashlib.sha256(
            canonical_json(
                {
                    "qids": [qid],
                    "people_columns": payload["people_columns"],
                    "people": [
                        row for row in payload["people"] if row["wikidata_id"] == qid
                    ],
                    "review_columns": payload["review_columns"],
                    "reviews": [
                        row for row in payload["reviews"] if row["wikidata_id"] == qid
                    ],
                    "proposals": [
                        item
                        for item in payload["proposals"]
                        if item["wikidata_id"] == qid
                    ],
                }
            ).encode()
        ).hexdigest()
        for qid in payload["qids"]
    }


def prepare_tranche_review(
    *,
    cohort_value: Path,
    tranche: int,
    proposals_path: Path,
    staged_people_csv: Path,
    staged_review_csv: Path,
) -> dict[str, Any]:
    """Freeze the exact staged rows and proposals presented for approval."""
    run_dir, cohort = cohort_paths(cohort_value)
    status = scheduler_status(cohort_value=cohort_value)
    tranche_status = next(
        (item for item in status["tranches"] if item["tranche"] == tranche), None
    )
    if tranche_status is None or not tranche_status["reviewable"]:
        raise BatchError(f"Approval tranche {tranche} is not reviewable")
    exception_qids = _exception_qids(
        {
            str(item["task_key"]): item
            for item in status["exceptions"]
        }
    )
    # Corrections are tied to an already-reviewed item and are not Part 2
    # vocabulary/semantic exceptions.  Keep them out of the exception-review
    # coverage set; they remain durable lane entries and are handled by the
    # correction workflow separately.
    exception_review_qids = _exception_qids(
        {
            str(item["task_key"]): item
            for item in status["exceptions"]
            if item.get("role") in {*SEMANTIC_ROLES, "vocabulary"}
        }
    )
    migrated_qids: set[str] = set()
    for migration_path in sorted((run_dir / "scheduler" / "migrations").glob("*.json")):
        migration = json.loads(migration_path.read_text(encoding="utf-8"))
        migrated_qids.update(str(qid) for qid in migration.get("qids", []))
    qids = [
        person["wikidata_id"]
        for person in cohort["selected"]
        if int(person.get("approval_tranche", 1)) == tranche
        and person["wikidata_id"] not in exception_qids
        and person["wikidata_id"] not in migrated_qids
    ]
    if not qids:
        raise BatchError(f"Approval tranche {tranche} has no unmigrated ready QIDs")
    payload = _review_payload(
        qids=qids,
        proposals_path=proposals_path,
        staged_people_csv=staged_people_csv,
        staged_review_csv=staged_review_csv,
    )
    review_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    item_hashes = _review_item_hashes(payload)
    manifest = {
        "schema_version": 1,
        "cohort_hash": cohort["cohort_hash"],
        "tranche": tranche,
        "qids": qids,
        "exceptions_excluded": sorted(exception_review_qids & {
            person["wikidata_id"]
            for person in cohort["selected"]
            if int(person.get("approval_tranche", 1)) == tranche
        }),
        "proposals": str(proposals_path),
        "staged_people_csv": str(staged_people_csv),
        "staged_review_csv": str(staged_review_csv),
        "review_hash": review_hash,
        "item_hashes": item_hashes,
        "prepared_utc": utc_now(),
        "catch_up": bool(migrated_qids),
    }
    review_root = run_dir / "scheduler" / "reviews"
    base = review_root / f"tranche-{tranche:03d}.json"
    path = base
    sequence = 1
    while path.exists():
        path = review_root / f"tranche-{tranche:03d}-catchup-{sequence:02d}.json"
        sequence += 1
    atomic_write_json(path, manifest)
    return {"review_manifest": str(path), **manifest}


def prepare_exception_review(
    *,
    cohort_value: Path,
    tranche: int,
    candidate_proposals: Path,
    staged_people_csv: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    """Freeze a separate, candidate-backed review for tranche exceptions."""
    run_dir, cohort = cohort_paths(cohort_value)
    people_fields, people_rows = read_csv(staged_people_csv)
    del people_fields
    people_by_qid = {row["wikidata_id"]: row for row in people_rows}
    tranche_qids = {
        person["wikidata_id"]
        for person in cohort["selected"]
        if int(person.get("approval_tranche", 1)) == tranche
    }
    exceptions = _scheduler_exceptions(run_dir)
    candidate_value = json.loads(candidate_proposals.read_text(encoding="utf-8"))
    candidate_rows = candidate_value.get("rows") if isinstance(candidate_value, dict) else candidate_value
    if not isinstance(candidate_rows, list):
        raise BatchError("Exception candidate proposals must contain a rows array")
    by_key = {}
    for item in candidate_rows:
        if not isinstance(item, dict):
            raise BatchError("Malformed exception candidate proposal")
        key = (str(item.get("exception_key", "")), str(item.get("qid", "")))
        if key in by_key:
            raise BatchError(f"Duplicate exception candidate proposal: {key[0]}:{key[1]}")
        by_key[key] = item

    rows: list[dict[str, Any]] = []
    for task_key, exception in exceptions.items():
        role = str(exception.get("role", ""))
        if role not in {*SEMANTIC_ROLES, "vocabulary"}:
            continue
        affected = [
            qid for qid in exception.get("affected_qids", []) if qid in tranche_qids
        ]
        if not affected and role in SEMANTIC_ROLES and exception.get("key") in tranche_qids:
            affected = [str(exception["key"])]
        for qid in affected:
            person = people_by_qid.get(qid)
            if person is None:
                raise BatchError(f"Exception QID missing from staged people CSV: {qid}")
            proposed = by_key.get((task_key, qid))
            if proposed is None:
                raise BatchError(f"Missing candidate proposal for {task_key}:{qid}")
            field = str(exception.get("field", ""))
            label = str(exception.get("label", ""))
            if role == "vocabulary" and (not field or not label):
                prefix, _, suffix = str(exception.get("key", "")).partition(":")
                field, label = prefix, suffix
            rows.append(
                {
                    "exception_key": task_key,
                    "qid": qid,
                    "name": person["name"],
                    "wikipedia_url": person["wikipedia_url"],
                    "field": field or role,
                    "label": label,
                    "reason": str(exception.get("reason", "")),
                    "proposed_mappings": proposed.get("proposed_mappings", []),
                }
            )
    rows.sort(key=lambda item: (item["field"].casefold(), item["name"].casefold(), item["qid"]))
    payload = {
        "schema_version": 1,
        "cohort_hash": cohort["cohort_hash"],
        "tranche": tranche,
        "rows": rows,
    }
    review_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    manifest = {**payload, "review_hash": review_hash, "prepared_utc": utc_now()}
    if output is None:
        output = run_dir / "scheduler" / "reviews" / f"tranche-{tranche:03d}-exceptions.json"
    atomic_write_json(output, manifest)

    def markdown_cell(value: Any) -> str:
        return str(value or "").replace("|", "\\|").replace("\n", " ")

    markdown_rows = [
        "# Tranche exception review",
        "",
        f"Review hash: `{review_hash}`",
        "",
        "| Name (Wikipedia link) | Problematic field | Original label | Reason | Proposed mappings |",
        "|---|---|---|---|---|",
    ]
    for item in rows:
        mapping_text = []
        for mapping in item["proposed_mappings"]:
            qid = str(mapping.get("qid", "")).strip()
            qid_display = (
                f"[{qid}](https://www.wikidata.org/wiki/{qid})"
                if QID_RE.fullmatch(qid)
                else "QID lookup needed"
            )
            mapping_text.append(
                f"{mapping.get('label', '')} ({qid_display}): {mapping.get('rationale', '')}"
            )
        mappings = "; ".join(mapping_text)
        linked_name = f"[{item['name']}]({item['wikipedia_url']})"
        markdown_rows.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    linked_name,
                    item["field"],
                    item["label"],
                    item["reason"],
                    mappings,
                )
            )
            + " |"
        )
    markdown_path = output.with_suffix(".md")
    markdown_path.write_text("\n".join(markdown_rows) + "\n", encoding="utf-8")
    return {
        "exception_review": str(output),
        "markdown": str(markdown_path),
        "review_hash": review_hash,
        "rows": len(rows),
    }


def queue_review_correction(
    *,
    cohort_value: Path,
    review_manifest: Path,
    qid: str,
    reason: str,
) -> dict[str, Any]:
    """Move one reviewed QID aside without invalidating accepted review items."""
    if not reason.strip():
        raise BatchError("Queueing a correction requires a reason")
    run_dir, cohort = cohort_paths(cohort_value)
    review = json.loads(review_manifest.read_text(encoding="utf-8"))
    if (
        review.get("schema_version") != 1
        or review.get("cohort_hash") != cohort["cohort_hash"]
        or qid not in review.get("qids", [])
    ):
        raise BatchError("Correction QID does not belong to this frozen tranche review")
    if qid not in review.get("item_hashes", {}):
        payload = _review_payload(
            qids=review["qids"],
            proposals_path=Path(review["proposals"]),
            staged_people_csv=Path(review["staged_people_csv"]),
            staged_review_csv=Path(review["staged_review_csv"]),
        )
        review["item_hashes"] = _review_item_hashes(payload)
        atomic_write_json(review_manifest, review)
    approval_path = review_manifest.with_name(review_manifest.stem + "-approval.json")
    if approval_path.exists():
        raise BatchError("Cannot queue a correction after tranche approval")
    task_key = _scheduler_key("correction", qid)
    exceptions = _scheduler_exceptions(run_dir)
    existing = exceptions.get(task_key)
    if existing is not None:
        if existing.get("review_manifest") != str(review_manifest):
            raise BatchError(f"Correction already exists for {qid}")
        return existing
    correction = {
        "task_key": task_key,
        "role": "correction",
        "key": qid,
        "reason": reason.strip(),
        "attempts": 0,
        "entered_utc": utc_now(),
        "affected_qids": [qid],
        "review_manifest": str(review_manifest),
        "review_hash": review["review_hash"],
    }
    exceptions[task_key] = correction
    atomic_write_json(run_dir / "scheduler" / "exceptions.json", exceptions)
    return correction


def record_tranche_approval(*, review_manifest: Path, reviewed_hash: str) -> Path:
    """Record the exact review hash only after genuine user approval."""
    review = json.loads(review_manifest.read_text(encoding="utf-8"))
    if review.get("schema_version") != 1 or reviewed_hash != review.get("review_hash"):
        raise BatchError("Approval hash does not match the reviewed tranche")
    exception_qids = set(review.get("exceptions_excluded", []))
    if exception_qids:
        exception_review_path = review_manifest.with_name(
            review_manifest.stem + "-exceptions.json"
        )
        if not exception_review_path.exists():
            raise BatchError(
                "Exception review is required before approving a tranche with exclusions"
            )
        exception_review = json.loads(exception_review_path.read_text(encoding="utf-8"))
        reviewed_exception_qids = {
            str(item.get("qid", "")) for item in exception_review.get("rows", [])
        }
        if (
            exception_review.get("schema_version") != 1
            or exception_review.get("cohort_hash") != review.get("cohort_hash")
            or int(exception_review.get("tranche", -1)) != int(review.get("tranche", -2))
            or reviewed_exception_qids != exception_qids
        ):
            raise BatchError("Exception review does not match the tranche exclusions")
    path = review_manifest.with_name(review_manifest.stem + "-approval.json")
    if path.exists():
        raise BatchError(f"Approval already exists: {path}")
    run_dir = review_manifest.parent.parent.parent
    corrections = {
        str(item.get("key")): item
        for item in _scheduler_exceptions(run_dir).values()
        if item.get("role") == "correction"
        and item.get("review_manifest") == str(review_manifest)
    }
    correction_qids = [qid for qid in review["qids"] if qid in corrections]
    approved_qids = [qid for qid in review["qids"] if qid not in corrections]
    if not approved_qids:
        raise BatchError("Reviewed tranche has no accepted QIDs to approve")
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "review_manifest": str(review_manifest),
            "review_hash": reviewed_hash,
            "approved_qids": approved_qids,
            "correction_qids": correction_qids,
            "approved_item_hashes": (
                {qid: review["item_hashes"][qid] for qid in approved_qids}
                if review.get("item_hashes")
                else {}
            ),
            "approved_utc": utc_now(),
        },
    )
    return path


def _validated_approval(
    *, cohort: Mapping[str, Any], approval_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    review_path = Path(str(approval.get("review_manifest", "")))
    if approval.get("schema_version") != 1 or not review_path.exists():
        raise BatchError("Invalid tranche approval")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if (
        review.get("schema_version") != 1
        or review.get("cohort_hash") != cohort["cohort_hash"]
        or approval.get("review_hash") != review.get("review_hash")
    ):
        raise BatchError("Approval does not match this frozen cohort review")
    approved_qids = list(approval.get("approved_qids", review["qids"]))
    correction_qids = list(approval.get("correction_qids", []))
    if (
        set(approved_qids) & set(correction_qids)
        or set(approved_qids) | set(correction_qids) != set(review["qids"])
    ):
        raise BatchError("Approval membership does not match the reviewed tranche")
    approved_item_hashes = approval.get("approved_item_hashes") or {}
    if approved_item_hashes:
        payload = _review_payload(
            qids=approved_qids,
            proposals_path=Path(review["proposals"]),
            staged_people_csv=Path(review["staged_people_csv"]),
            staged_review_csv=Path(review["staged_review_csv"]),
        )
        current_item_hashes = _review_item_hashes(payload)
        for qid in approved_qids:
            if current_item_hashes[qid] != approved_item_hashes.get(qid):
                raise BatchError(f"Reviewed item changed after approval: {qid}")
    else:
        payload = _review_payload(
            qids=review["qids"],
            proposals_path=Path(review["proposals"]),
            staged_people_csv=Path(review["staged_people_csv"]),
            staged_review_csv=Path(review["staged_review_csv"]),
        )
        current_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        if current_hash != review["review_hash"]:
            raise BatchError("Reviewed tranche changed after approval")
    return approval, review


def migrate_approved_cohort(
    *,
    cohort_value: Path,
    proposals_path: Path,
    staged_people_csv: Path,
    staged_review_csv: Path,
    live_people_csv: Path,
    live_review_csv: Path,
    removed_csv: Path,
    cache_root: Path,
    approval_path: Path | None = None,
) -> dict[str, Any]:
    """Migrate only the approved cohort, permanently removing its removals.

    The staged files are a full snapshot, but migration patches only selected
    QIDs into the current live files. This preserves unrelated live changes and
    makes the removal ledger append-only and idempotent.
    """

    run_dir, cohort = cohort_paths(cohort_value)
    review: dict[str, Any] | None = None
    if cohort.get("selection_mode") == "all_eligible":
        if approval_path is None:
            raise BatchError("All-eligible queues require an exact tranche approval")
        approval, review = _validated_approval(
            cohort=cohort, approval_path=approval_path
        )
        proposals_path = Path(review["proposals"])
        staged_people_csv = Path(review["staged_people_csv"])
        staged_review_csv = Path(review["staged_review_csv"])
        selected_qids = list(approval.get("approved_qids", review["qids"]))
    else:
        selected_qids = [person["wikidata_id"] for person in cohort["selected"]]
    validation = validate_proposals(
        cohort_value=cohort_value,
        proposals_path=proposals_path,
        people_csv=staged_people_csv,
        cache_root=cache_root,
        expected_qids=selected_qids,
        allow_extra_proposals=review is not None,
    )
    staged_fields, staged_rows = read_csv(staged_people_csv)
    live_fields, live_rows = read_csv(live_people_csv)
    if staged_fields != list(cohort["public_csv_columns"]):
        raise BatchError("Staged people CSV schema does not match cohort")
    if live_fields != staged_fields:
        raise BatchError("Live people CSV schema differs from staged CSV")
    _, removed_rows = load_removed_entries(removed_csv, live_fields)
    removed_by_qid = {row["wikidata_id"]: row for row in removed_rows}
    verify_protected(cohort, live_fields, live_rows, removed_rows=removed_by_qid)
    validate_public_rows(staged_fields, staged_rows)
    validate_public_rows(live_fields, live_rows)

    staged_by_qid = {row["wikidata_id"]: row for row in staged_rows}
    live_by_qid = {row["wikidata_id"]: row for row in live_rows}
    proposals = {
        proposal["wikidata_id"]: proposal
        for proposal in load_proposals(proposals_path)
    }
    missing_staged = [qid for qid in selected_qids if qid not in staged_by_qid]
    if missing_staged:
        raise BatchError(f"Selected QIDs missing from staged CSV: {missing_staged}")

    removal_qids: list[str] = []
    ledger_additions: list[dict[str, str]] = []
    removed_at = utc_now()
    for qid in selected_qids:
        proposal = proposals[qid]
        if proposal["status"] != "possible_removal":
            if qid not in live_by_qid:
                raise BatchError(f"Selected non-removal QID is absent from live CSV: {qid}")
            live_by_qid[qid] = dict(staged_by_qid[qid])
            continue

        eligibility = proposal.get("article_eligibility") or {}
        reasons = eligibility.get("removal_reasons")
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(reason not in REMOVAL_REASONS for reason in reasons)
        ):
            raise BatchError(f"{qid}: possible removal has no valid removal reason")
        reason_text = "; ".join(str(reason) for reason in reasons)
        existing = removed_by_qid.get(qid)
        if existing is not None:
            if existing.get("removal_reason") != reason_text:
                raise BatchError(f"Removed-entry reason changed for {qid}")
            if qid in live_by_qid:
                raise BatchError(f"Removed QID is still present in live CSV: {qid}")
            removal_qids.append(qid)
            continue
        if qid not in live_by_qid:
            raise BatchError(f"Selected removal QID is absent from live CSV: {qid}")
        ledger_row = {column: str(staged_by_qid[qid].get(column, "")) for column in live_fields}
        ledger_row.update(
            {
                "removal_reason": reason_text,
                "removed_utc": removed_at,
                "source_run_dir": str(run_dir),
            }
        )
        ledger_additions.append(ledger_row)
        removed_by_qid[qid] = ledger_row
        live_by_qid.pop(qid)
        removal_qids.append(qid)

    live_rows_out = [live_by_qid[row["wikidata_id"]] for row in live_rows if row["wikidata_id"] in live_by_qid]
    validate_public_rows(live_fields, live_rows_out)

    live_review_fields, live_review_rows = read_csv(live_review_csv)
    staged_review_fields, staged_review_rows = read_csv(staged_review_csv)
    if live_review_fields != REVIEW_COLUMNS or staged_review_fields != REVIEW_COLUMNS:
        raise BatchError("Review queue schema mismatch")
    existing_review = {row["wikidata_id"]: row for row in live_review_rows}
    if len(existing_review) != len(live_review_rows):
        raise BatchError("Duplicate live review queue QID")
    staged_review_by_qid = {row["wikidata_id"]: row for row in staged_review_rows}
    if len(staged_review_by_qid) != len(staged_review_rows):
        raise BatchError("Duplicate staged review queue QID")
    removal_set = set(removal_qids)
    for qid in selected_qids:
        existing_review.pop(qid, None)
        if qid not in removal_set and qid in staged_review_by_qid:
            existing_review[qid] = staged_review_by_qid[qid]
    review_rows_out = [existing_review[qid] for qid in sorted(existing_review)]

    ledger_rows = removed_rows + ledger_additions
    validate_public_rows(
        live_fields,
        [{column: row.get(column, "") for column in live_fields} for row in ledger_rows],
    )
    validate_removed_entry_rows(ledger_rows)

    # Validate every destination before replacing any live file.
    atomic_write_csv(live_people_csv, live_fields, live_rows_out)
    atomic_write_csv(live_review_csv, REVIEW_COLUMNS, review_rows_out)
    atomic_write_csv(removed_csv, removed_entry_columns(live_fields), ledger_rows)
    cohort["stages"]["live_migration_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    result = {
        "migrated_utc": cohort["stages"]["live_migration_complete_utc"],
        "selected": len(selected_qids),
        "qids": selected_qids,
        "migrated_rows": len(selected_qids) - len(removal_qids),
        "removed_rows": len(ledger_additions),
        "already_ledgered_rows": len(removal_qids) - len(ledger_additions),
        "live_people_rows": len(live_rows_out),
        "live_review_queue_rows": len(review_rows_out),
        "removed_entries_csv": str(removed_csv),
        "validation": validation,
    }
    if review is None:
        atomic_write_json(run_dir / "migration.json", result)
    else:
        atomic_write_json(
            run_dir
            / "scheduler"
            / "migrations"
            / f'{Path(approval["review_manifest"]).stem}.json',
            {**result, "approval": str(approval_path), "review_hash": review["review_hash"]},
        )
    return result


def reconcile_legacy_removals(
    *,
    proposal_paths: Sequence[Path],
    live_people_csv: Path,
    live_review_csv: Path,
    removed_csv: Path,
) -> dict[str, Any]:
    """Backfill the ledger for already-approved legacy possible removals."""

    fields, live_rows = read_csv(live_people_csv)
    validate_public_rows(fields, live_rows)
    _, ledger_rows = load_removed_entries(removed_csv, fields)
    ledger_by_qid = {row["wikidata_id"]: row for row in ledger_rows}
    live_by_qid = {row["wikidata_id"]: row for row in live_rows}
    additions: list[dict[str, str]] = []
    removal_qids: set[str] = set()
    for proposals_path in proposal_paths:
        run_dir = proposals_path.parent
        for proposal in load_proposals(proposals_path):
            if proposal.get("status") != "possible_removal":
                continue
            qid = str(proposal.get("wikidata_id", ""))
            eligibility = proposal.get("article_eligibility") or {}
            reasons = eligibility.get("removal_reasons")
            if (
                not isinstance(reasons, list)
                or not reasons
                or any(reason not in REMOVAL_REASONS for reason in reasons)
            ):
                raise BatchError(f"{qid}: legacy removal has no valid removal reason")
            reason_text = "; ".join(str(reason) for reason in reasons)
            if qid in ledger_by_qid:
                if ledger_by_qid[qid].get("removal_reason") != reason_text:
                    raise BatchError(f"Legacy removal reason changed for {qid}")
                continue
            if qid not in live_by_qid:
                raise BatchError(f"Legacy removal QID is absent from live CSV: {qid}")
            ledger_row = {column: str(live_by_qid[qid].get(column, "")) for column in fields}
            ledger_row.update(
                {
                    "removal_reason": reason_text,
                    "removed_utc": utc_now(),
                    "source_run_dir": str(run_dir),
                }
            )
            additions.append(ledger_row)
            ledger_by_qid[qid] = ledger_row
            live_by_qid.pop(qid)
            removal_qids.add(qid)

    if not additions:
        return {"removed_rows": 0, "live_people_rows": len(live_rows)}
    live_rows_out = [
        live_by_qid[row["wikidata_id"]]
        for row in live_rows
        if row["wikidata_id"] in live_by_qid
    ]
    validate_public_rows(fields, live_rows_out)
    review_fields, review_rows = read_csv(live_review_csv)
    if review_fields != REVIEW_COLUMNS:
        raise BatchError("Review queue schema mismatch")
    review_out = [row for row in review_rows if row["wikidata_id"] not in removal_qids]
    ledger_out = ledger_rows + additions
    validate_removed_entry_rows(ledger_out)
    atomic_write_csv(live_people_csv, fields, live_rows_out)
    atomic_write_csv(live_review_csv, REVIEW_COLUMNS, review_out)
    atomic_write_csv(removed_csv, removed_entry_columns(fields), ledger_out)
    return {
        "removed_rows": len(additions),
        "live_people_rows": len(live_rows_out),
        "live_review_queue_rows": len(review_out),
        "removed_entries_csv": str(removed_csv),
    }


def verify_batch(
    *,
    cohort_value: Path,
    people_csv: Path,
    musicians_csv: Path,
    review_csv: Path,
    rebuild_browser: bool,
    run_tests: bool,
    removed_csv: Path | None = None,
    allow_removed: bool = False,
    expected_qids: Sequence[str] | None = None,
) -> dict[str, Any]:
    run_dir, cohort = cohort_paths(cohort_value)
    fieldnames, rows = read_csv(people_csv)
    removed_by_qid: dict[str, dict[str, str]] = {}
    if allow_removed:
        if removed_csv is None:
            raise BatchError("--allow-removed requires --removed-csv")
        _, removed_rows = load_removed_entries(removed_csv, fieldnames)
        removed_by_qid = {row["wikidata_id"]: row for row in removed_rows}
    verify_protected(cohort, fieldnames, rows, removed_rows=removed_by_qid)
    validate_public_rows(fieldnames, rows)
    by_qid = {row["wikidata_id"]: row for row in rows}
    selected_people = [
        person
        for person in cohort["selected"]
        if expected_qids is None or person["wikidata_id"] in set(expected_qids)
    ]
    if expected_qids is not None and {
        person["wikidata_id"] for person in selected_people
    } != set(expected_qids):
        raise BatchError("Verification QIDs are outside the frozen cohort")
    incomplete = [
        person["wikidata_id"]
        for person in selected_people
        if person["wikidata_id"] not in removed_by_qid
        and not row_is_terminal(by_qid[person["wikidata_id"]])
    ]
    if incomplete:
        raise BatchError(f"Selected people remain incomplete: {incomplete}")

    review_fields, review_rows = read_csv(review_csv)
    if review_fields != REVIEW_COLUMNS:
        raise BatchError("Review queue schema mismatch")
    review_qids = [row["wikidata_id"] for row in review_rows]
    if len(review_qids) != len(set(review_qids)):
        raise BatchError("Duplicate review queue QID")
    removed_in_review = sorted(set(removed_by_qid) & set(review_qids))
    if removed_in_review:
        raise BatchError(f"Removed QIDs remain in review queue: {removed_in_review}")

    _, musicians = read_csv(musicians_csv)
    missing_musicians = [
        row["wikidata_id"]
        for row in musicians
        if row["wikidata_id"] not in by_qid
        and row["wikidata_id"] not in removed_by_qid
    ]
    mismatched_musicians = [
        row["wikidata_id"]
        for row in musicians
        if row["wikidata_id"] in by_qid
        and any(
            row[column] != by_qid[row["wikidata_id"]][column]
            for column in (
                "birth_date",
                "death_date",
                "age_status",
                "minimum_lifespan_days",
                "maximum_lifespan_days",
                "possible_age_range",
            )
        )
    ]
    if missing_musicians or mismatched_musicians:
        raise BatchError(
            f"Musician invariants failed; missing={missing_musicians}, "
            f"mismatched={mismatched_musicians}"
        )

    deterministic = None
    if rebuild_browser:
        subprocess.run([sys.executable, str(BROWSER_BUILDER)], check=True, cwd=REPO_ROOT)
        first = hashlib.sha256(BROWSER_DATA.read_bytes()).hexdigest()
        subprocess.run([sys.executable, str(BROWSER_BUILDER)], check=True, cwd=REPO_ROOT)
        second = hashlib.sha256(BROWSER_DATA.read_bytes()).hexdigest()
        deterministic = first == second
        if not deterministic:
            raise BatchError("Browser payload rebuild is not deterministic")

    test_results = []
    if run_tests:
        test_dirs = (
            PROJECT_DIR / "tests",
            REPO_ROOT / "age-27-musicians" / "tests",
            REPO_ROOT / "age-27-browser" / "tests",
        )

        def run_suite(test_dir: Path) -> dict[str, Any]:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(test_dir),
                    "-v",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
            )
            if completed.returncode:
                raise BatchError(
                    f"Tests failed in {test_dir}:\n"
                    f"{completed.stdout}\n{completed.stderr}"
                )
            match = re.search(r"Ran (\d+) tests?", completed.stderr)
            return {
                "directory": str(test_dir),
                "tests": int(match.group(1)) if match else None,
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {test_dir: executor.submit(run_suite, test_dir) for test_dir in test_dirs}
            test_results = [futures[test_dir].result() for test_dir in test_dirs]
        diff_check = subprocess.run(
            ["git", "diff", "--check"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
        )
        if diff_check.returncode:
            raise BatchError(f"git diff --check failed:\n{diff_check.stdout}")

    stage_key = (
        "final_validation_complete_utc"
        if expected_qids is None
        else "partial_validation_complete_utc"
    )
    cohort["stages"][stage_key] = utc_now()
    if expected_qids is None:
        cohort["run_ended_utc"] = cohort["stages"][stage_key]
    atomic_write_json(run_dir / "cohort.json", cohort)
    result = {
        "selected": len(selected_people),
        "completed": len(selected_people) - len(incomplete),
        "removed_rows": len(
            set(person["wikidata_id"] for person in selected_people) & set(removed_by_qid)
        ),
        "people_rows": len(rows),
        "musician_rows": len(musicians),
        "review_queue_rows": len(review_rows),
        "browser_deterministic": deterministic,
        "tests": test_results,
    }
    atomic_write_json(run_dir / "verification.json", result)
    return result


def _ambiguous_scheduler_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "ambiguity" / "scheduler" / "state.json"
    if not path.exists():
        return {
            "schema_version": 1,
            "next_assignment": 1,
            "active_leases": {},
            "attempts": {},
            "completed_assignments": [],
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "next_assignment",
        "active_leases",
        "attempts",
        "completed_assignments",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1:
        raise BatchError("Invalid ambiguous-member scheduler state")
    return value


def _ambiguous_exceptions(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = run_dir / "ambiguity" / "scheduler" / "exceptions.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BatchError("Invalid ambiguous-member exception lane")
    return value


def _ambiguous_candidate_record(run_dir: Path, qid: str) -> dict[str, Any]:
    path = run_dir / "ambiguity" / "candidates" / f"{qid}.json"
    if not path.exists():
        raise BatchError(f"Missing ambiguous-member scan for {qid}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("wikidata_id") != qid:
        raise BatchError(f"Invalid ambiguous-member scan for {qid}")
    return value


def _ambiguous_semantic_path(run_dir: Path, qid: str) -> Path:
    return run_dir / "ambiguity" / "semantic" / f"{qid}.json"


def build_ambiguous_role_input(
    *, cohort_value: Path, qid: str, output: Path | None = None
) -> Path:
    run_dir, cohort = _require_ambiguous_cohort(cohort_value)
    person = next(
        (item for item in cohort["selected"] if item["wikidata_id"] == qid), None
    )
    if person is None:
        raise BatchError(f"QID is outside ambiguous-member cohort: {qid}")
    record = _ambiguous_candidate_record(run_dir, qid)
    semantic_ids = set(
        record.get("pending_semantic_candidate_ids", record["semantic_candidate_ids"])
    )
    if not semantic_ids:
        raise BatchError(f"{qid} has no candidates requiring semantic review")
    payload = {
        "schema_version": 1,
        "role": "ambiguous-member-review",
        "wikidata_id": qid,
        "name": person["name"],
        "article_url": record["article_url"],
        "revision_id": record["revision_id"],
        "member_classes": person["member_classes"],
        "existing": {
            "age_status": person["age_status"],
            "birth_dates": split_values(person["birth_date"]),
            "death_dates": split_values(person["death_date"]),
        },
        "policy": {
            "age_numbers": [26, 27, 28],
            "age_question": "Does this exact excerpt explicitly state the subject's age at death?",
            "date_question": "Does this exact lead excerpt explicitly give the subject's birth or death date?",
            "do_not_infer": True,
            "canonical_infobox_age": record["canonical_infobox_age"],
        },
        "candidates": [
            candidate
            for candidate in record["candidates"]
            if candidate["candidate_id"] in semantic_ids
        ],
    }
    output = output or run_dir / "ambiguity" / "inputs" / f"{qid}.json"
    atomic_write_json(output, payload)
    return output


def _validate_ambiguous_semantic_result(
    run_dir: Path,
    qid: str,
    value: Mapping[str, Any],
    *,
    expected_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    record = _ambiguous_candidate_record(run_dir, qid)
    required = {"schema_version", "wikidata_id", "candidate_reviews", "reason"}
    if set(value) != required or value.get("schema_version") != 1:
        raise BatchError(f"{qid}: malformed ambiguous-member semantic result")
    if value.get("wikidata_id") != qid:
        raise BatchError(f"{qid}: semantic result QID mismatch")
    if not isinstance(value.get("reason"), str) or not str(value["reason"]).strip():
        raise BatchError(f"{qid}: blank semantic review reason")
    reviews = value.get("candidate_reviews")
    if not isinstance(reviews, list) or any(not isinstance(item, dict) for item in reviews):
        raise BatchError(f"{qid}: candidate_reviews must be an array")
    expected = set(expected_ids or record["semantic_candidate_ids"])
    received = [str(item.get("candidate_id", "")) for item in reviews]
    if set(received) != expected or len(received) != len(set(received)):
        raise BatchError(f"{qid}: semantic result must review every candidate exactly once")
    candidate_by_id = {item["candidate_id"]: item for item in record["candidates"]}
    normalized = []
    for item in reviews:
        if set(item) != {"candidate_id", "verdict", "claim_type", "reason"}:
            raise BatchError(f"{qid}: malformed candidate review")
        candidate_id = str(item["candidate_id"])
        verdict = str(item["verdict"])
        claim_type = str(item["claim_type"])
        reason = str(item["reason"]).strip()
        candidate = candidate_by_id[candidate_id]
        if verdict not in AMBIGUOUS_VERDICTS or claim_type not in AMBIGUOUS_CLAIM_TYPES:
            raise BatchError(f"{qid}: invalid verdict or claim type for {candidate_id}")
        if not reason:
            raise BatchError(f"{qid}: blank reason for {candidate_id}")
        if candidate["kind"] == "age" and claim_type not in {"age_at_death", "other"}:
            raise BatchError(f"{qid}: age candidate has incompatible claim type")
        if candidate["kind"] == "date" and claim_type not in {
            "birth_date",
            "death_date",
            "other",
        }:
            raise BatchError(f"{qid}: date candidate has incompatible claim type")
        if verdict == "confirmed" and claim_type == "other":
            raise BatchError(f"{qid}: confirmed candidate cannot have claim_type other")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "verdict": verdict,
                "claim_type": claim_type,
                "reason": reason,
            }
        )
    return {
        "schema_version": 1,
        "wikidata_id": qid,
        "candidate_reviews": normalized,
        "reason": str(value["reason"]).strip(),
    }


def ambiguous_scheduler_status(*, cohort_value: Path) -> dict[str, Any]:
    run_dir, cohort = _require_ambiguous_cohort(cohort_value)
    if "ambiguity_scan_complete_utc" not in cohort.get("stages", {}):
        raise BatchError("Run scan-ambiguous before querying its scheduler")
    state = _ambiguous_scheduler_state(run_dir)
    exceptions = _ambiguous_exceptions(run_dir)
    active_qids = {
        qid
        for lease in state["active_leases"].values()
        for qid in lease.get("qids", [])
    }
    finalized_qids: set[str] = set()
    finalized_tranches: set[int] = set()
    for path in sorted((run_dir / "ambiguity" / "reviews").glob("tranche-*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        finalized_qids.update(str(qid) for qid in manifest.get("finalized_qids", []))
        finalized_tranches.add(int(manifest["tranche"]))
    ready_items: list[str] = []
    tranches: dict[int, dict[str, Any]] = {}
    remaining = 0
    for person in cohort["selected"]:
        qid = person["wikidata_id"]
        number = int(person["approval_tranche"])
        tranche = tranches.setdefault(
            number,
            {
                "tranche": number,
                "total": 0,
                "deterministic_only": 0,
                "semantic_complete": 0,
                "exceptions": 0,
                "pending": 0,
                "finalized": 0,
            },
        )
        tranche["total"] += 1
        if qid in finalized_qids:
            tranche["finalized"] += 1
            continue
        record = _ambiguous_candidate_record(run_dir, qid)
        if qid in exceptions:
            tranche["exceptions"] += 1
            continue
        semantic_ids = record.get(
            "pending_semantic_candidate_ids", record["semantic_candidate_ids"]
        )
        if not semantic_ids:
            tranche["deterministic_only"] += 1
            continue
        if _ambiguous_semantic_path(run_dir, qid).exists():
            tranche["semantic_complete"] += 1
            continue
        tranche["pending"] += 1
        remaining += 1
        if qid not in active_qids and int(state["attempts"].get(qid, 0)) < MAX_TASK_ATTEMPTS:
            ready_items.append(qid)
    for tranche in tranches.values():
        tranche["reviewable"] = (
            tranche["pending"] == 0
            and tranche["tranche"] not in finalized_tranches
        )
    usage_path = run_dir / "ambiguity" / "scheduler" / "usage.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else []
    return {
        "lane": "ambiguous_members",
        "cohort": str(run_dir),
        "slot_limit": MAX_SEMANTIC_SLOTS,
        "active_leases": state["active_leases"],
        "ready_count": len(ready_items),
        "ready_items": ready_items,
        "exceptions": list(exceptions.values()),
        "tranches": [tranches[number] for number in sorted(tranches)],
        "usage": usage,
        "processing_remaining": remaining,
        "processing_complete": remaining == 0 and not state["active_leases"],
        "completion_scope": "frozen_ambiguous_cohort_only",
    }


def claim_ambiguous_assignment(*, cohort_value: Path, slot: int) -> dict[str, Any]:
    if slot not in range(1, MAX_SEMANTIC_SLOTS + 1):
        raise BatchError(f"Slot must be 1..{MAX_SEMANTIC_SLOTS}")
    run_dir, _ = _require_ambiguous_cohort(cohort_value)
    state = _ambiguous_scheduler_state(run_dir)
    slot_key = str(slot)
    if slot_key in state["active_leases"]:
        raise BatchError(f"Slot {slot} already has an active lease")
    if len(state["active_leases"]) >= MAX_SEMANTIC_SLOTS:
        raise BatchError("All semantic slots are leased")
    status = ambiguous_scheduler_status(cohort_value=cohort_value)
    packed: list[dict[str, Any]] = []
    total_bytes = 0
    for qid in status["ready_items"]:
        input_path = build_ambiguous_role_input(cohort_value=cohort_value, qid=qid)
        size = input_path.stat().st_size
        if packed and (
            len(packed) >= AMBIGUOUS_ASSIGNMENT_LIMITS["max_items"]
            or total_bytes + size > AMBIGUOUS_ASSIGNMENT_LIMITS["max_bytes"]
        ):
            break
        packed.append(
            {
                "key": qid,
                "input": str(input_path),
                "input_bytes": size,
                "oversize_singleton": not packed
                and size > AMBIGUOUS_ASSIGNMENT_LIMITS["max_bytes"],
            }
        )
        total_bytes += size
        if size > AMBIGUOUS_ASSIGNMENT_LIMITS["max_bytes"]:
            break
    if not packed:
        return {"assignment": None, **status}
    assignment_id = f'AR{int(state["next_assignment"]):06d}'
    manifest = {
        "schema_version": 1,
        "assignment_id": assignment_id,
        "slot": slot,
        "role": "ambiguous-member-review",
        "prompt": str(PROJECT_DIR / "semantic-prompts" / "ambiguous-member-review.md"),
        "items": packed,
        "input_bytes": total_bytes,
        "max_input_bytes": AMBIGUOUS_ASSIGNMENT_LIMITS["max_bytes"],
        "max_items": AMBIGUOUS_ASSIGNMENT_LIMITS["max_items"],
        "result_format": "JSON array containing exactly one result object per item",
        "claimed_utc": utc_now(),
    }
    manifest_path = (
        run_dir / "ambiguity" / "scheduler" / "assignments" / f"{assignment_id}.json"
    )
    atomic_write_json(manifest_path, manifest)
    qids = [item["key"] for item in packed]
    for qid in qids:
        state["attempts"][qid] = int(state["attempts"].get(qid, 0)) + 1
    state["next_assignment"] += 1
    state["active_leases"][slot_key] = {
        "assignment_id": assignment_id,
        "manifest": str(manifest_path),
        "qids": qids,
    }
    atomic_write_json(run_dir / "ambiguity" / "scheduler" / "state.json", state)
    return {"assignment": str(manifest_path), **manifest}


def complete_ambiguous_assignment(
    *,
    cohort_value: Path,
    assignment_id: str,
    input_path: Path | None,
    failed_reason: str = "",
    recorded_input_tokens: int | None = None,
    recorded_output_tokens: int | None = None,
) -> dict[str, Any]:
    run_dir, _ = _require_ambiguous_cohort(cohort_value)
    state = _ambiguous_scheduler_state(run_dir)
    leases = [
        (slot, lease)
        for slot, lease in state["active_leases"].items()
        if lease.get("assignment_id") == assignment_id
    ]
    if len(leases) != 1:
        raise BatchError(f"Assignment is not actively leased: {assignment_id}")
    slot, lease = leases[0]
    manifest = json.loads(Path(lease["manifest"]).read_text(encoding="utf-8"))
    if any(
        value is not None and value < 0
        for value in (recorded_input_tokens, recorded_output_tokens)
    ):
        raise BatchError("Recorded token counts cannot be negative")
    results: list[dict[str, Any]] = []
    parse_error = ""
    if input_path is not None:
        try:
            results = _load_assignment_result(input_path)
        except (BatchError, json.JSONDecodeError, OSError) as exc:
            parse_error = str(exc)
    elif not failed_reason.strip():
        raise BatchError("Completion requires --input or --failed-reason")
    by_qid: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for result in results:
        qid = str(result.get("wikidata_id", ""))
        if qid in by_qid:
            duplicates.add(qid)
        else:
            by_qid[qid] = result
    expected = [item["key"] for item in manifest["items"]]
    extras = sorted(set(by_qid) - set(expected))
    installed: list[str] = []
    failed: list[dict[str, Any]] = []
    exceptions = _ambiguous_exceptions(run_dir)
    for qid in expected:
        reason = (
            failed_reason.strip()
            or parse_error
            or (f"duplicate result key: {qid}" if qid in duplicates else "")
            or "assignment omitted this item"
        )
        result = by_qid.get(qid)
        if result is not None and qid not in duplicates and not parse_error:
            try:
                record = _ambiguous_candidate_record(run_dir, qid)
                pending_ids = record.get(
                    "pending_semantic_candidate_ids",
                    record["semantic_candidate_ids"],
                )
                pending = _validate_ambiguous_semantic_result(
                    run_dir, qid, result, expected_ids=pending_ids
                )
                normalized = _validate_ambiguous_semantic_result(
                    run_dir,
                    qid,
                    {
                        "schema_version": 1,
                        "wikidata_id": qid,
                        "candidate_reviews": [
                            *record.get("reused_candidate_reviews", []),
                            *pending["candidate_reviews"],
                        ],
                        "reason": pending["reason"],
                    },
                )
            except BatchError as exc:
                reason = str(exc)
            else:
                atomic_write_json(_ambiguous_semantic_path(run_dir, qid), normalized)
                installed.append(qid)
                continue
        attempts = int(state["attempts"].get(qid, 0))
        failure = {"key": qid, "reason": reason, "attempts": attempts}
        failed.append(failure)
        if attempts >= MAX_TASK_ATTEMPTS:
            exceptions[qid] = {
                "role": "ambiguous-member-review",
                "key": qid,
                "reason": reason,
                "attempts": attempts,
                "entered_utc": utc_now(),
            }
    state["active_leases"].pop(slot)
    state["completed_assignments"].append(assignment_id)
    atomic_write_json(run_dir / "ambiguity" / "scheduler" / "state.json", state)
    atomic_write_json(run_dir / "ambiguity" / "scheduler" / "exceptions.json", exceptions)
    usage_path = run_dir / "ambiguity" / "scheduler" / "usage.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else []
    usage.append(
        {
            "assignment_id": assignment_id,
            "items": len(expected),
            "serialized_input_bytes": manifest["input_bytes"],
            "result_bytes": input_path.stat().st_size if input_path and input_path.exists() else None,
            "recorded_input_tokens": recorded_input_tokens,
            "recorded_output_tokens": recorded_output_tokens,
            "completed_utc": utc_now(),
        }
    )
    atomic_write_json(usage_path, usage)
    return {
        "assignment_id": assignment_id,
        "installed": installed,
        "failed": failed,
        "exceptions_added": [item for item in failed if item["key"] in exceptions],
        "rejected_extra_keys": extras,
        "slot_released": int(slot),
    }


def _best_unique_date(values: Sequence[str]) -> str:
    valid = [value for value in values if _date_precision(value)]
    if not valid:
        return ""
    precision = max(_date_precision(value) for value in valid)
    best = sorted({value for value in valid if _date_precision(value) == precision})
    return best[0] if len(best) == 1 else ""


def _single_age_from_dates(birth: str, death: str) -> int | None:
    if not birth or not death:
        return None
    low, high = _age_bounds(birth, death)
    return low if low == high else None


def _date_value_supports_age(birth: str, death: str, age: int) -> bool:
    low, high = _age_bounds(birth, death)
    return low <= age <= high


def _assemble_ambiguous_review_row(
    run_dir: Path, person: Mapping[str, Any]
) -> dict[str, str] | None:
    qid = str(person["wikidata_id"])
    record = _ambiguous_candidate_record(run_dir, qid)
    semantic: dict[str, Any] | None = None
    semantic_path = _ambiguous_semantic_path(run_dir, qid)
    if semantic_path.exists():
        semantic = _validate_ambiguous_semantic_result(
            run_dir, qid, json.loads(semantic_path.read_text(encoding="utf-8"))
        )
    reviews = {
        item["candidate_id"]: item for item in (semantic or {}).get("candidate_reviews", [])
    }
    evidence: list[tuple[dict[str, Any], str]] = []
    ambiguous_evidence: list[dict[str, Any]] = []
    for candidate in record["candidates"]:
        if not candidate["requires_semantic_review"]:
            evidence.append((candidate, str(candidate["deterministic_claim_type"])))
            continue
        review = reviews.get(candidate["candidate_id"])
        if review and review["verdict"] == "confirmed":
            evidence.append((candidate, review["claim_type"]))
        elif review and review["verdict"] == "ambiguous":
            ambiguous_evidence.append(candidate)
    if not evidence and not ambiguous_evidence:
        return None

    conflict_reasons: list[str] = []
    material_conflicts: list[str] = []
    canonical_age = record.get("canonical_infobox_age")
    explicit_ages = sorted(
        {
            int(candidate["value"])
            for candidate, claim_type in evidence
            if claim_type == "age_at_death" and candidate["kind"] == "age"
        }
    )
    confirmed_age: int | None = explicit_ages[0] if len(explicit_ages) == 1 else None

    infobox_range = sorted({int(value) for value in record.get("ambiguous_infobox_ages", [])})
    if infobox_range and confirmed_age is not None and not (
        min(infobox_range) <= confirmed_age <= max(infobox_range)
    ):
        conflict_reasons.append(
            "singular age differs from the non-singular infobox age range "
            f"{min(infobox_range)}-{max(infobox_range)}"
        )

    recommended_dates: dict[str, str] = {"birth_date": "", "death_date": ""}
    best_date_candidates: dict[str, list[str]] = {"birth_date": [], "death_date": []}
    for field in ("birth_date", "death_date"):
        current = record[f"wikidata_{field}s"]
        minimum_precision = max((_date_precision(value) for value in current), default=0)
        candidates = [
            str(candidate["value"])
            for candidate, claim_type in evidence
            if claim_type == field and _date_precision(str(candidate["value"])) >= minimum_precision
        ]
        if candidates:
            best_precision = max(_date_precision(value) for value in candidates)
            best = sorted({value for value in candidates if _date_precision(value) == best_precision})
            best_date_candidates[field] = best
            if len(best) == 1:
                recommended_dates[field] = best[0]

    # A confirmed age can select a date alternative only when exactly one value
    # remains compatible with the other field.
    if confirmed_age is not None:
        for field, other in (("birth_date", "death_date"), ("death_date", "birth_date")):
            values = best_date_candidates[field]
            if len(values) < 2:
                continue
            other_value = recommended_dates[other] or _best_unique_date(
                record[f"wikidata_{other}s"]
            )
            if not other_value:
                continue
            compatible = [
                value
                for value in values
                if _date_value_supports_age(
                    value if field == "birth_date" else other_value,
                    other_value if field == "birth_date" else value,
                    confirmed_age,
                )
            ]
            if len(compatible) == 1:
                recommended_dates[field] = compatible[0]

    for field in ("birth_date", "death_date"):
        if len(best_date_candidates[field]) > 1 and not recommended_dates[field]:
            reason = f"conflicting equal-precision Wikipedia {field} values"
            conflict_reasons.append(reason)

    birth_for_age = recommended_dates["birth_date"] or _best_unique_date(
        record["wikidata_birth_dates"]
    )
    death_for_age = recommended_dates["death_date"] or _best_unique_date(
        record["wikidata_death_dates"]
    )
    calculated_age = _single_age_from_dates(birth_for_age, death_for_age)
    singular_ages = set(explicit_ages)
    if calculated_age is not None:
        singular_ages.add(calculated_age)
    if len(singular_ages) == 1:
        confirmed_age = next(iter(singular_ages))
    elif len(singular_ages) > 1:
        confirmed_age = None
        reason = "conflicting singular age-at-death candidates: " + ", ".join(
            str(age) for age in sorted(singular_ages)
        )
        conflict_reasons.append(reason)
        material_conflicts.append(reason)

    action = "none"
    age_status = str(person["age_status"])
    date_update = any(
        (
            recommended_dates[field]
            and recommended_dates[field] not in record[f"wikidata_{field}s"]
        )
        or (
            recommended_dates[field]
            and len(record[f"wikidata_{field}s"]) > 1
        )
        for field in ("birth_date", "death_date")
    )
    if material_conflicts:
        action = "review"
    elif confirmed_age is not None and confirmed_age != 27:
        action = "discard"
    elif confirmed_age == 27 and age_status == "possible":
        action = "elevate"
    elif date_update:
        action = "update"
    if action not in AMBIGUOUS_ACTIONS:
        raise BatchError(f"{qid}: invalid ambiguous-member action")

    sources = []
    excerpts = []
    for candidate, _ in evidence:
        source = str(candidate["source"])
        excerpt = str(candidate["excerpt"])
        if source not in sources:
            sources.append(source)
        if excerpt and excerpt not in excerpts:
            excerpts.append(excerpt)
    for candidate in ambiguous_evidence:
        source = str(candidate["source"])
        if source not in sources:
            sources.append(source)
        excerpt = str(candidate["excerpt"])
        if excerpt and excerpt not in excerpts:
            excerpts.append(excerpt)
    reason_parts = []
    if canonical_age is not None:
        reason_parts.append("single explicit infobox age is canonical")
    elif confirmed_age is not None:
        reason_parts.append("explicit evidence or accepted dates establish one age")
    if date_update:
        reason_parts.append("Wikipedia supplies an equal-or-better-precision date update")
    if conflict_reasons:
        reason_parts.extend(conflict_reasons)
    if not reason_parts:
        reason_parts.append("evidence does not support a membership or date change")
    return {
        "wikidata_id": qid,
        "name": str(person["name"]),
        "article_url": str(record["article_url"]),
        "revision_id": str(record["revision_id"]),
        "member_classes": "; ".join(person["member_classes"]),
        "existing_age_status": age_status,
        "wikidata_birth_dates": "; ".join(record["wikidata_birth_dates"]),
        "wikidata_death_dates": "; ".join(record["wikidata_death_dates"]),
        "source_row_sha256": str(person["source_row_sha256"]),
        "confirmed_age_at_death": "" if confirmed_age is None else str(confirmed_age),
        "recommended_birth_date": recommended_dates["birth_date"],
        "recommended_death_date": recommended_dates["death_date"],
        "evidence_sources": "; ".join(sources),
        "evidence_excerpts": " | ".join(excerpts),
        "conflict": "; ".join(dict.fromkeys(conflict_reasons)),
        "recommended_action": action,
        "reason": "; ".join(reason_parts),
        "reviewed_utc": utc_now(),
        "source_run_dir": str(run_dir),
    }


def finalize_ambiguous_tranche(
    *,
    cohort_value: Path,
    tranche: int,
    review_csv: Path,
    state_path: Path | None,
) -> dict[str, Any]:
    run_dir, cohort = _require_ambiguous_cohort(cohort_value)
    state_path = state_path or Path(str(cohort["state_path"]))
    status = ambiguous_scheduler_status(cohort_value=cohort_value)
    tranche_status = next(
        (item for item in status["tranches"] if item["tranche"] == tranche), None
    )
    if tranche_status is None:
        raise BatchError(f"Unknown ambiguous-member tranche: {tranche}")
    if not tranche_status["reviewable"]:
        raise BatchError(f"Ambiguous-member tranche {tranche} is not reviewable")
    exceptions = _ambiguous_exceptions(run_dir)
    people = [
        person
        for person in cohort["selected"]
        if int(person["approval_tranche"]) == tranche
    ]
    report_rows = [
        row
        for person in people
        if person["wikidata_id"] not in exceptions
        for row in [_assemble_ambiguous_review_row(run_dir, person)]
        if row is not None
    ]
    if review_csv.exists():
        fields, existing = read_csv(review_csv)
        if fields != AMBIGUOUS_REVIEW_COLUMNS:
            raise BatchError("Ambiguous-member review CSV schema mismatch")
    else:
        existing = []
    finalized_qids = {person["wikidata_id"] for person in people if person["wikidata_id"] not in exceptions}
    merged = [row for row in existing if row["wikidata_id"] not in finalized_qids]
    merged.extend(report_rows)
    merged.sort(key=lambda row: (row["name"].casefold(), row["wikidata_id"]))
    atomic_write_csv(review_csv, AMBIGUOUS_REVIEW_COLUMNS, merged)

    state = _load_ambiguous_state(state_path)
    if (
        state.get("schema_version") == 1
        or state.get("review_policy_version") != AMBIGUOUS_REVIEW_POLICY_VERSION
    ):
        selected_qids = {person["wikidata_id"] for person in cohort["selected"]}
        grandfathered = sorted(set(state["members"]) - selected_qids)
        state["schema_version"] = 2
        state["review_policy_version"] = AMBIGUOUS_REVIEW_POLICY_VERSION
        state["grandfathered_completion_count"] = len(grandfathered)
        state["grandfathered_completion_qids_sha256"] = hashlib.sha256(
            "\n".join(grandfathered).encode()
        ).hexdigest()
    for person in people:
        qid = person["wikidata_id"]
        if qid in exceptions:
            continue
        record = _ambiguous_candidate_record(run_dir, qid)
        state["members"][qid] = {
            "source_row_sha256": person["source_row_sha256"],
            "revision_id": record["revision_id"],
            "outcome": "reported" if any(row["wikidata_id"] == qid for row in report_rows) else "no_reportable_evidence",
            "reviewed_utc": utc_now(),
            "source_run_dir": str(run_dir),
            "review_policy_version": AMBIGUOUS_REVIEW_POLICY_VERSION,
        }
    atomic_write_json(state_path, state)
    payload = {
        "schema_version": 1,
        "lane": "ambiguous_members",
        "tranche": tranche,
        "qids": [person["wikidata_id"] for person in people],
        "finalized_qids": sorted(finalized_qids),
        "exception_qids": sorted(set(exceptions) & {person["wikidata_id"] for person in people}),
        "report_rows": report_rows,
        "report_sha256": hashlib.sha256(canonical_json(report_rows).encode()).hexdigest(),
        "review_csv": str(review_csv),
        "state_path": str(state_path),
        "created_utc": utc_now(),
    }
    manifest_path = run_dir / "ambiguity" / "reviews" / f"tranche-{tranche:03d}.json"
    atomic_write_json(manifest_path, payload)
    markdown = [
        f"# Ambiguous-member Wikipedia review tranche {tranche}",
        "",
        f"Report hash: `{payload['report_sha256']}`",
        "",
        "| Name | Evidence | Age | Birth | Death | Action | Conflict |",
        "|---|---|---:|---|---|---|---|",
    ]
    for row in report_rows:
        markdown.append(
            "| [{name}]({url}) | {sources} | {age} | {birth} | {death} | {action} | {conflict} |".format(
                name=row["name"].replace("|", "\\|"),
                url=row["article_url"],
                sources=row["evidence_sources"].replace("|", "\\|"),
                age=row["confirmed_age_at_death"],
                birth=row["recommended_birth_date"],
                death=row["recommended_death_date"],
                action=row["recommended_action"],
                conflict=row["conflict"].replace("|", "\\|"),
            )
        )
    if payload["exception_qids"]:
        markdown.extend(
            ["", "Exceptions: " + ", ".join(payload["exception_qids"])]
        )
    atomic_write_text(manifest_path.with_suffix(".md"), "\n".join(markdown) + "\n")
    return {
        "tranche": tranche,
        "finalized": len(finalized_qids),
        "report_rows": len(report_rows),
        "exceptions": len(payload["exception_qids"]),
        "report_sha256": payload["report_sha256"],
        "manifest": str(manifest_path),
        "markdown": str(manifest_path.with_suffix(".md")),
    }


def verify_ambiguous_repair(
    *,
    cohort_value: Path,
    manifest_path: Path,
    original_review_csv: Path,
    original_state_path: Path,
    staged_review_csv: Path,
    staged_state_path: Path,
) -> dict[str, Any]:
    manifest = _load_ambiguous_repair_manifest(manifest_path)
    if hashlib.sha256(original_review_csv.read_bytes()).hexdigest() != manifest["report_sha256"]:
        raise BatchError("Original ambiguous-member review CSV changed after repair freeze")
    if hashlib.sha256(original_state_path.read_bytes()).hexdigest() != manifest["state_sha256"]:
        raise BatchError("Original ambiguous-member state changed after repair freeze")
    original_fields, original_rows = read_csv(original_review_csv)
    staged_fields, staged_rows = read_csv(staged_review_csv)
    if original_fields != AMBIGUOUS_REVIEW_COLUMNS or staged_fields != original_fields:
        raise BatchError("Ambiguous-member repair CSV schema mismatch")
    original_by_qid = {row["wikidata_id"]: row for row in original_rows}
    staged_by_qid = {row["wikidata_id"]: row for row in staged_rows}
    if len(original_by_qid) != len(original_rows) or len(staged_by_qid) != len(staged_rows):
        raise BatchError("Duplicate QID in ambiguous-member repair CSV")
    target_rows = _mixed_repair_targets(manifest)
    target_qids = {item["wikidata_id"] for item in target_rows}
    if (set(staged_by_qid) - set(original_by_qid)) - target_qids:
        raise BatchError("Repair introduced an unlisted report QID")
    preserved = manifest["preserved_report_rows"]
    for qid, expected_hash in preserved.items():
        row = staged_by_qid.get(qid)
        if row is None or _ambiguous_review_row_sha256(row) != expected_hash:
            raise BatchError(f"Repair changed an approved report row: {qid}")
    changed_qids = {
        qid
        for qid in set(original_by_qid) | set(staged_by_qid)
        if original_by_qid.get(qid) != staged_by_qid.get(qid)
    }
    if not changed_qids.issubset(target_qids):
        raise BatchError("Repair changed a QID outside its target manifest")
    if any(row["recommended_action"] not in AMBIGUOUS_ACTIONS for row in staged_rows):
        raise BatchError("Repair produced an invalid recommended action")

    original_state = _load_ambiguous_state(original_state_path)
    staged_state = _load_ambiguous_state(staged_state_path)
    for qid, value in original_state["members"].items():
        if qid not in target_qids and staged_state["members"].get(qid) != value:
            raise BatchError(f"Repair changed non-target completion state: {qid}")
    status = ambiguous_scheduler_status(cohort_value=cohort_value)
    if not status["processing_complete"] or status["active_leases"]:
        raise BatchError("Ambiguous-member repair scheduler is not complete")
    if status["exceptions"]:
        raise BatchError("Ambiguous-member repair has unresolved exceptions")
    if any(not tranche["finalized"] == tranche["total"] for tranche in status["tranches"]):
        raise BatchError("Ambiguous-member repair has an unfinalized tranche")

    if manifest.get("lane") == "ambiguous_members_mixed_repair":
        if len(preserved) != 960 or len(manifest["preserved_state_members"]) != 1192:
            raise BatchError("Mixed repair preservation count mismatch")
        for item in target_rows:
            qid = item["wikidata_id"]
            original_state_entry = original_state["members"].get(qid)
            if (
                original_state_entry is None
                or hashlib.sha256(canonical_json(original_state_entry).encode()).hexdigest()
                != item["state_entry_sha256"]
            ):
                raise BatchError(f"Original mixed repair state target changed: {qid}")
            original_report = original_by_qid.get(qid)
            if item["target_class"] == "current_none":
                if (
                    original_report is None
                    or original_report["recommended_action"] != "none"
                    or _ambiguous_review_row_sha256(original_report) != item["report_row_sha256"]
                ):
                    raise BatchError(f"Original current-none repair target changed: {qid}")
            elif original_report is not None or original_state_entry.get("outcome") != "no_reportable_evidence":
                raise BatchError(f"Original grandfathered repair target changed: {qid}")
        for qid, expected_hash in manifest["preserved_state_members"].items():
            entry = staged_state["members"].get(qid)
            if entry is None or hashlib.sha256(canonical_json(entry).encode()).hexdigest() != expected_hash:
                raise BatchError(f"Repair changed protected state entry: {qid}")

    deltas: dict[str, int] = {}
    for qid in target_qids:
        before = original_by_qid.get(qid, {}).get("recommended_action", "absent")
        # An absent grandfathered row remains absent when the completed repair
        # still has no reportable evidence; it was never a removable report row.
        after = staged_by_qid.get(qid, {}).get(
            "recommended_action", "absent" if before == "absent" else "removed"
        )
        key = f"{before}->{after}"
        deltas[key] = deltas.get(key, 0) + 1
    return {
        "target_count": len(target_qids),
        "changed_target_count": len(changed_qids),
        "preserved_report_rows": len(preserved),
        "preserved_state_members": len(original_state["members"]) - len(target_qids),
        "staged_report_rows": len(staged_rows),
        "action_deltas": dict(sorted(deltas.items())),
    }


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source.read_bytes())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def publish_ambiguous_repair(**kwargs: Any) -> dict[str, Any]:
    verification = verify_ambiguous_repair(**kwargs)
    _atomic_copy(kwargs["staged_review_csv"], kwargs["original_review_csv"])
    _atomic_copy(kwargs["staged_state_path"], kwargs["original_state_path"])
    verification["published_review_sha256"] = hashlib.sha256(
        kwargs["original_review_csv"].read_bytes()
    ).hexdigest()
    verification["published_state_sha256"] = hashlib.sha256(
        kwargs["original_state_path"].read_bytes()
    ).hexdigest()
    return verification


def _structured_time_from_display(value: str) -> StructuredTime:
    match = DATE_RE.fullmatch(value)
    if not match:
        raise BatchError(f"Invalid date value: {value}")
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    precision = 11 if match.group(3) else 10 if match.group(2) else 9
    raw = f"{'-' if year < 0 else '+'}{abs(year):04d}-{month:02d}-{day:02d}T00:00:00Z"
    return StructuredTime(raw=raw, precision=precision, calendar=GREGORIAN)


def _recalculate_age_columns(row: dict[str, str]) -> None:
    births = [_structured_time_from_display(value) for value in split_values(row["birth_date"])]
    deaths = [_structured_time_from_display(value) for value in split_values(row["death_date"])]
    birth_bounds = [value.bounds() for value in births]
    death_bounds = [value.bounds() for value in deaths]
    if not birth_bounds or not death_bounds:
        raise BatchError(f"{row['wikidata_id']}: birth and death dates are required")
    earliest_birth = min((start for start, _ in birth_bounds), key=lambda value: value.to_jdn())
    latest_birth = max((end for _, end in birth_bounds), key=lambda value: value.to_jdn())
    earliest_death = min((start for start, _ in death_bounds), key=lambda value: value.to_jdn())
    latest_death = max((end for _, end in death_bounds), key=lambda value: value.to_jdn())
    minimum_days = earliest_death.to_jdn() - latest_birth.to_jdn()
    maximum_days = latest_death.to_jdn() - earliest_birth.to_jdn()
    if minimum_days < 0 or maximum_days < minimum_days:
        raise BatchError(f"{row['wikidata_id']}: recommended dates permit invalid chronology")
    minimum_age = calendar_age(latest_birth, earliest_death)
    maximum_age = calendar_age(earliest_birth, latest_death)
    low = format_calendar_age(*minimum_age)
    high = format_calendar_age(*maximum_age)
    row["minimum_lifespan_days"] = str(minimum_days)
    row["maximum_lifespan_days"] = str(maximum_days)
    row["possible_age_range"] = low if low == high else f"{low} to {high}"


def apply_ambiguous_recommendations(
    *, review_csv: Path, people_csv: Path, musicians_csv: Path, removed_csv: Path
) -> dict[str, Any]:
    """Apply the report's discard, elevate, and update actions to public data."""

    review_fields, reviews = read_csv(review_csv)
    if review_fields != AMBIGUOUS_REVIEW_COLUMNS:
        raise BatchError("Ambiguous-member review schema mismatch")
    review_qids = [row["wikidata_id"] for row in reviews]
    if len(review_qids) != len(set(review_qids)):
        raise BatchError("Duplicate ambiguous-member review QID")

    people_fields, people_rows = read_csv(people_csv)
    musicians_fields, musician_rows = read_csv(musicians_csv)
    validate_public_rows(people_fields, people_rows)
    people_by_qid = {row["wikidata_id"]: dict(row) for row in people_rows}
    musicians_by_qid = {row["wikidata_id"]: dict(row) for row in musician_rows}
    expected_removed_fields = removed_entry_columns(people_fields)
    if removed_csv.exists():
        removed_fields, removed_rows = read_csv(removed_csv)
    else:
        removed_fields, removed_rows = expected_removed_fields, []
    legacy_removed_fields = [
        column for column in expected_removed_fields if column != "description"
    ]
    if removed_fields == legacy_removed_fields:
        removed_rows = [
            {column: row.get(column, "") for column in expected_removed_fields}
            for row in removed_rows
        ]
    elif removed_fields != expected_removed_fields:
        raise BatchError(f"Removed-entry ledger schema mismatch: {removed_csv}")
    validate_removed_entry_rows(removed_rows)

    counts = {action: 0 for action in AMBIGUOUS_ACTIONS}
    prepared: dict[str, dict[str, str]] = {}
    for review in reviews:
        qid = review["wikidata_id"]
        action = review["recommended_action"]
        if action not in AMBIGUOUS_ACTIONS:
            raise BatchError(f"{qid}: invalid ambiguous-member action {action!r}")
        counts[action] += 1
        person = people_by_qid.get(qid)
        if person is None:
            raise BatchError(f"{qid}: review row is missing from people CSV")
        if _ambiguous_source_row_sha256(person) != review["source_row_sha256"]:
            raise BatchError(f"{qid}: review source row changed")
        candidate = dict(person)
        if action == "elevate":
            if person["age_status"] != "possible" or review["confirmed_age_at_death"] != "27":
                raise BatchError(f"{qid}: invalid elevate recommendation")
            candidate["age_status"] = "confirmed"
        elif action == "update":
            if not review["recommended_birth_date"] and not review["recommended_death_date"]:
                raise BatchError(f"{qid}: update recommendation has no date")
            if review["recommended_birth_date"]:
                candidate["birth_date"] = review["recommended_birth_date"]
            if review["recommended_death_date"]:
                candidate["death_date"] = review["recommended_death_date"]
            _recalculate_age_columns(candidate)
        prepared[qid] = candidate

    removed_at = utc_now()
    removal_qids: set[str] = set()
    ledger_additions: list[dict[str, str]] = []
    existing_removed = {row["wikidata_id"] for row in removed_rows}
    for review in reviews:
        qid = review["wikidata_id"]
        action = review["recommended_action"]
        person = prepared[qid]
        if action == "discard":
            if qid in existing_removed:
                raise BatchError(f"{qid}: discard is already present in removed entries")
            ledger_row = {column: people_by_qid[qid].get(column, "") for column in people_fields}
            ledger_row.update(
                {
                    "removal_reason": f"ambiguous-member review: {review['reason']}",
                    "removed_utc": removed_at,
                    "source_run_dir": review["source_run_dir"],
                }
            )
            ledger_additions.append(ledger_row)
            removal_qids.add(qid)
        elif action in {"elevate", "update"}:
            people_by_qid[qid] = person
            musician = musicians_by_qid.get(qid)
            if musician is not None:
                for column in (
                    "birth_date", "death_date", "age_status", "minimum_lifespan_days",
                    "maximum_lifespan_days", "possible_age_range",
                ):
                    musician[column] = person[column]

    people_out = [people_by_qid[row["wikidata_id"]] for row in people_rows if row["wikidata_id"] not in removal_qids]
    musicians_out = [musicians_by_qid[row["wikidata_id"]] for row in musician_rows if row["wikidata_id"] not in removal_qids]
    ledger_out = removed_rows + ledger_additions
    validate_public_rows(people_fields, people_out)
    validate_removed_entry_rows(ledger_out)
    if len({row["wikidata_id"] for row in musicians_out}) != len(musicians_out):
        raise BatchError("Duplicate musician QID after ambiguous-member migration")

    atomic_write_csv(people_csv, people_fields, people_out)
    atomic_write_csv(musicians_csv, musicians_fields, musicians_out)
    atomic_write_csv(removed_csv, removed_entry_columns(people_fields), ledger_out)
    return {
        "review_rows": len(reviews), "action_counts": counts,
        "people_rows": len(people_out), "musician_rows": len(musicians_out),
        "removed_rows": len(ledger_additions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--people-csv", type=Path, default=PEOPLE_CSV)
    parser.add_argument("--cache-root", type=Path, default=CACHE_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser(
        "status", help="Read-only effective-eligibility count and next rows"
    )
    status.add_argument("--limit", type=int, default=3)
    status.add_argument("--target-manifest", type=Path)
    status.add_argument("--removed-csv", type=Path, default=REMOVED_ENTRIES_CSV)

    select = subparsers.add_parser("select", help="Freeze the next eligible cohort")
    selection = select.add_mutually_exclusive_group()
    selection.add_argument("--batch-size", type=int)
    selection.add_argument(
        "--all-eligible",
        action="store_true",
        help="Freeze the complete eligible queue; approval remains in 100-person tranches",
    )
    select.add_argument("--run-dir", type=Path)
    select.add_argument("--target-manifest", type=Path)
    select.add_argument("--removed-csv", type=Path, default=REMOVED_ENTRIES_CSV)

    ambiguous_status_parser = subparsers.add_parser(
        "ambiguous-status",
        help="Read-only count of live possible or multi-date ambiguous members",
    )
    ambiguous_status_parser.add_argument("--limit", type=int, default=3)
    ambiguous_status_parser.add_argument("--state", type=Path)
    ambiguous_status_parser.add_argument("--rescan-all", action="store_true")

    ambiguous_select = subparsers.add_parser(
        "select-ambiguous",
        help="Freeze the independent ambiguous-member Wikipedia review lane",
    )
    ambiguous_select.add_argument("--batch-size", type=int)
    ambiguous_select.add_argument("--run-dir", type=Path)
    ambiguous_select.add_argument("--state", type=Path)
    ambiguous_select.add_argument("--rescan-all", action="store_true")
    ambiguous_select.add_argument("--target-manifest", type=Path)
    ambiguous_select.add_argument(
        "--review-csv", type=Path, default=AMBIGUOUS_REVIEW_CSV
    )

    ambiguous_prepare_repair = subparsers.add_parser(
        "prepare-ambiguous-repair",
        help="Freeze an exact action-scoped ambiguous-member repair manifest",
    )
    ambiguous_prepare_repair.add_argument(
        "--review-csv", type=Path, default=AMBIGUOUS_REVIEW_CSV
    )
    ambiguous_prepare_repair.add_argument("--state", type=Path)
    ambiguous_prepare_repair.add_argument("--action", choices=sorted(AMBIGUOUS_ACTIONS), required=True)
    ambiguous_prepare_repair.add_argument("--expected-count", type=int, required=True)
    ambiguous_prepare_repair.add_argument("--output", type=Path, required=True)

    fetch = subparsers.add_parser("fetch", help="Bulk-fetch/cache current articles")
    fetch.add_argument("--cohort", type=Path, required=True)
    fetch.add_argument("--max-cache-age-hours", type=float, default=24)

    alternates = subparsers.add_parser(
        "fetch-alternates",
        help="Fetch every non-English sitelink for rejected English pages",
    )
    alternates.add_argument("--cohort", type=Path, required=True)
    alternates.add_argument(
        "--proposals",
        type=Path,
        help="Legacy proposal input; omit to use semantic eligibility artifacts",
    )

    packetize = subparsers.add_parser(
        "packetize", help="Create complete hierarchical semantic packets"
    )
    packetize.add_argument("--cohort", type=Path, required=True)

    ambiguous_scan = subparsers.add_parser(
        "scan-ambiguous",
        help="Deterministically scan visible prose, citation titles, lead, and infobox",
    )
    ambiguous_scan.add_argument("--cohort", type=Path, required=True)

    ambiguous_reuse = subparsers.add_parser(
        "reuse-ambiguous-reviews",
        help="Reuse unchanged non-ambiguous semantic decisions from completed reviews",
    )
    ambiguous_reuse.add_argument("--cohort", type=Path, required=True)
    ambiguous_reuse.add_argument("--state", type=Path)
    ambiguous_reuse.add_argument(
        "--from-current-policy-cohort",
        type=Path,
        help="Reuse matching decisions from a cohort already reviewed under the current policy",
    )

    ambiguous_role_input = subparsers.add_parser(
        "ambiguous-role-input",
        help="Build one narrow semantic input for ambiguous-member evidence hits",
    )
    ambiguous_role_input.add_argument("--cohort", type=Path, required=True)
    ambiguous_role_input.add_argument("--qid", required=True)
    ambiguous_role_input.add_argument("--output", type=Path)

    ambiguous_scheduler = subparsers.add_parser(
        "ambiguous-scheduler-status",
        help="Read-only refillable queue and tranche status for ambiguous members",
    )
    ambiguous_scheduler.add_argument("--cohort", type=Path, required=True)

    ambiguous_claim = subparsers.add_parser(
        "claim-ambiguous-assignment",
        help="Claim one byte-capped ambiguous-member semantic assignment",
    )
    ambiguous_claim.add_argument("--cohort", type=Path, required=True)
    ambiguous_claim.add_argument("--slot", type=int, required=True)

    ambiguous_complete = subparsers.add_parser(
        "complete-ambiguous-assignment",
        help="Validate ambiguous-member semantic results and release the slot",
    )
    ambiguous_complete.add_argument("--cohort", type=Path, required=True)
    ambiguous_complete.add_argument("--assignment-id", required=True)
    ambiguous_complete.add_argument("--recorded-input-tokens", type=int)
    ambiguous_complete.add_argument("--recorded-output-tokens", type=int)
    ambiguous_completion = ambiguous_complete.add_mutually_exclusive_group(required=True)
    ambiguous_completion.add_argument("--input", type=Path)
    ambiguous_completion.add_argument("--failed-reason")

    ambiguous_finalize = subparsers.add_parser(
        "finalize-ambiguous-tranche",
        help="Merge one reviewable tranche into the report-only manual-review CSV",
    )
    ambiguous_finalize.add_argument("--cohort", type=Path, required=True)
    ambiguous_finalize.add_argument("--tranche", type=int, required=True)
    ambiguous_finalize.add_argument(
        "--review-csv", type=Path, default=AMBIGUOUS_REVIEW_CSV
    )
    ambiguous_finalize.add_argument("--state", type=Path)

    ambiguous_apply = subparsers.add_parser(
        "apply-ambiguous-recommendations",
        help="Apply discard, elevate, and update actions from the ambiguous review CSV",
    )
    ambiguous_apply.add_argument(
        "--review-csv", type=Path, default=AMBIGUOUS_REVIEW_CSV
    )
    ambiguous_apply.add_argument("--musicians-csv", type=Path, default=MUSICIANS_CSV)
    ambiguous_apply.add_argument("--removed-csv", type=Path, default=REMOVED_ENTRIES_CSV)

    for command, help_text in (
        ("verify-ambiguous-repair", "Verify a staged targeted ambiguous-member repair"),
        ("publish-ambiguous-repair", "Atomically publish a verified ambiguous-member repair"),
    ):
        repair = subparsers.add_parser(command, help=help_text)
        repair.add_argument("--cohort", type=Path, required=True)
        repair.add_argument("--manifest", type=Path, required=True)
        repair.add_argument(
            "--original-review-csv", type=Path, default=AMBIGUOUS_REVIEW_CSV
        )
        repair.add_argument("--original-state", type=Path)
        repair.add_argument("--staged-review-csv", type=Path, required=True)
        repair.add_argument("--staged-state", type=Path, required=True)

    init = subparsers.add_parser(
        "init-proposals", help="Create the strict proposal JSONL template"
    )
    init.add_argument("--cohort", type=Path, required=True)
    init.add_argument("--output", type=Path)

    record = subparsers.add_parser(
        "record-artifact", help="Install one immutable role/QID semantic artifact"
    )
    record.add_argument("--cohort", type=Path, required=True)
    record.add_argument("--role", choices=SEMANTIC_ROLES, required=True)
    record.add_argument("--qid", required=True)
    record.add_argument("--input", type=Path, required=True)

    mark_task = subparsers.add_parser(
        "mark-task", help="Record controller-owned running/failed task state"
    )
    mark_task.add_argument("--cohort", type=Path, required=True)
    mark_task.add_argument("--role", choices=SEMANTIC_ROLES, required=True)
    mark_task.add_argument("--qid", required=True)
    mark_task.add_argument("--status", choices=sorted(TASK_STATES), required=True)
    mark_task.add_argument("--reason", default="")

    role_input = subparsers.add_parser(
        "role-input", help="Build one narrow deterministic semantic-agent input"
    )
    role_input.add_argument("--cohort", type=Path, required=True)
    role_input.add_argument("--role", choices=SEMANTIC_ROLES, required=True)
    role_input.add_argument("--qid", required=True)
    role_input.add_argument("--output", type=Path)
    role_input.add_argument("--vocabulary", type=Path)

    aggregate = subparsers.add_parser(
        "aggregate-eligibility",
        help="Select English/largest qualifying alternate from eligibility artifacts",
    )
    aggregate.add_argument("--cohort", type=Path, required=True)
    aggregate.add_argument(
        "--ready-only",
        action="store_true",
        help="Incrementally aggregate only QIDs whose required reviews are durable",
    )

    stages = subparsers.add_parser(
        "stage-status", help="Read-only per-QID semantic artifact readiness"
    )
    stages.add_argument("--cohort", type=Path, required=True)

    scheduler = subparsers.add_parser(
        "scheduler-status", help="Read-only refillable-slot and approval-tranche state"
    )
    scheduler.add_argument("--cohort", type=Path, required=True)

    claim = subparsers.add_parser(
        "claim-assignment", help="Claim one deterministic byte-capped semantic assignment"
    )
    claim.add_argument("--cohort", type=Path, required=True)
    claim.add_argument("--slot", type=int, required=True)
    claim.add_argument("--vocabulary", type=Path)

    complete = subparsers.add_parser(
        "complete-assignment", help="Validate an assignment result and release its slot"
    )
    complete.add_argument("--cohort", type=Path, required=True)
    complete.add_argument("--assignment-id", required=True)
    complete.add_argument("--recorded-input-tokens", type=int)
    complete.add_argument("--recorded-output-tokens", type=int)
    completion = complete.add_mutually_exclusive_group(required=True)
    completion.add_argument("--input", type=Path)
    completion.add_argument("--failed-reason")

    resolve_exception_parser = subparsers.add_parser(
        "resolve-exception", help="Return one diagnosed exception to the refillable queue"
    )
    resolve_exception_parser.add_argument("--cohort", type=Path, required=True)
    resolve_exception_parser.add_argument(
        "--role", choices=[*SCHEDULER_ROLES, "correction"], required=True
    )
    resolve_exception_parser.add_argument("--key", required=True)
    resolve_exception_parser.add_argument("--reason", required=True)

    assemble = subparsers.add_parser(
        "assemble", help="Assemble semantic artifacts into proposal JSONL"
    )
    assemble.add_argument("--cohort", type=Path, required=True)
    assemble.add_argument("--output", type=Path)
    assemble.add_argument("--tranche", type=int)

    resolve = subparsers.add_parser(
        "resolve-known", help="Fill proposal QIDs from established vocabulary"
    )
    resolve.add_argument("--proposals", type=Path, required=True)
    resolve.add_argument("--vocabulary", type=Path)
    resolve.add_argument("--trusted-vocabulary", type=Path, default=TRUSTED_VOCABULARY)

    lookup = subparsers.add_parser(
        "lookup-vocabulary",
        help="Cache Wikidata Search API candidates for unresolved labels",
    )
    lookup.add_argument("--proposals", type=Path, required=True)
    lookup.add_argument("--max-cache-age-hours", type=float, default=168)
    lookup.add_argument("--limit", type=int, default=8)

    record_vocab = subparsers.add_parser(
        "record-vocabulary", help="Install one immutable field/label vocabulary decision"
    )
    record_vocab.add_argument("--cohort", type=Path, required=True)
    record_vocab.add_argument("--input", type=Path, required=True)

    vocabulary_input = subparsers.add_parser(
        "vocabulary-input", help="Build one narrow unresolved-label agent input"
    )
    vocabulary_input.add_argument("--proposals", type=Path, required=True)
    vocabulary_input.add_argument("--field", choices=FIELD_SPECS, required=True)
    vocabulary_input.add_argument("--label", required=True)
    vocabulary_input.add_argument("--output", type=Path)

    apply_vocab = subparsers.add_parser(
        "apply-vocabulary", help="Apply approved novel QID decisions serially"
    )
    apply_vocab.add_argument("--cohort", type=Path, required=True)
    apply_vocab.add_argument("--proposals", type=Path, required=True)
    apply_vocab.add_argument("--vocabulary", type=Path)

    validate = subparsers.add_parser(
        "validate", help="Validate staged proposals without public writes"
    )
    validate.add_argument("--cohort", type=Path, required=True)
    validate.add_argument("--proposals", type=Path, required=True)
    validate.add_argument("--tranche", type=int)

    apply = subparsers.add_parser(
        "apply", help="Atomically apply validated proposals and merge review queue"
    )
    apply.add_argument("--cohort", type=Path, required=True)
    apply.add_argument("--proposals", type=Path, required=True)
    apply.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    apply.add_argument("--tranche", type=int)

    prepare_review = subparsers.add_parser(
        "prepare-tranche-review",
        help="Freeze the exact staged tranche payload presented for approval",
    )
    prepare_review.add_argument("--cohort", type=Path, required=True)
    prepare_review.add_argument("--tranche", type=int, required=True)
    prepare_review.add_argument("--proposals", type=Path, required=True)
    prepare_review.add_argument("--staged-people-csv", type=Path, required=True)
    prepare_review.add_argument("--staged-review-csv", type=Path, required=True)

    exception_review = subparsers.add_parser(
        "prepare-exception-review",
        help="Freeze a candidate-backed review for tranche exceptions",
    )
    exception_review.add_argument("--cohort", type=Path, required=True)
    exception_review.add_argument("--tranche", type=int, required=True)
    exception_review.add_argument("--candidate-proposals", type=Path, required=True)
    exception_review.add_argument("--staged-people-csv", type=Path, required=True)
    exception_review.add_argument("--output", type=Path)

    approve_review = subparsers.add_parser(
        "record-tranche-approval",
        help="Record the exact reviewed hash after explicit user approval",
    )
    approve_review.add_argument("--review-manifest", type=Path, required=True)
    approve_review.add_argument("--reviewed-hash", required=True)

    queue_correction = subparsers.add_parser(
        "queue-review-correction",
        help="Move one reviewed QID to the manual correction lane",
    )
    queue_correction.add_argument("--cohort", type=Path, required=True)
    queue_correction.add_argument("--review-manifest", type=Path, required=True)
    queue_correction.add_argument("--qid", required=True)
    queue_correction.add_argument("--reason", required=True)

    migrate = subparsers.add_parser(
        "migrate",
        help="Migrate an approved staged cohort and append removals to the ledger",
    )
    migrate.add_argument("--cohort", type=Path, required=True)
    migrate.add_argument("--proposals", type=Path, required=True)
    migrate.add_argument("--staged-people-csv", type=Path, required=True)
    migrate.add_argument("--staged-review-csv", type=Path, required=True)
    migrate.add_argument("--live-people-csv", type=Path, default=PEOPLE_CSV)
    migrate.add_argument("--live-review-csv", type=Path, default=REVIEW_CSV)
    migrate.add_argument("--removed-csv", type=Path, default=REMOVED_ENTRIES_CSV)
    migrate.add_argument(
        "--approval",
        type=Path,
        help="Required exact-hash approval for an all-eligible queue tranche",
    )

    reconcile = subparsers.add_parser(
        "reconcile-removals",
        help="Backfill the permanent ledger for already-approved legacy removals",
    )
    reconcile.add_argument("--proposals", type=Path, nargs="+", required=True)
    reconcile.add_argument("--live-people-csv", type=Path, default=PEOPLE_CSV)
    reconcile.add_argument("--live-review-csv", type=Path, default=REVIEW_CSV)
    reconcile.add_argument("--removed-csv", type=Path, default=REMOVED_ENTRIES_CSV)

    verify = subparsers.add_parser(
        "verify", help="Verify effective fields, browser, tests, and invariants"
    )
    verify.add_argument("--cohort", type=Path, required=True)
    verify.add_argument("--musicians-csv", type=Path, default=MUSICIANS_CSV)
    verify.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    verify.add_argument("--removed-csv", type=Path)
    verify.add_argument("--allow-removed", action="store_true")
    verify.add_argument("--rebuild-browser", action="store_true")
    verify.add_argument("--run-tests", action="store_true")
    verify.add_argument("--tranche", type=int)

    prepare = subparsers.add_parser(
        "prepare-redo", help="Back up and clear the currently enriched rows"
    )
    prepare.add_argument("--expected-count", type=int, required=True)
    prepare.add_argument("--backup-dir", type=Path, required=True)
    prepare.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    prepare.add_argument("--removed-csv", type=Path, default=REMOVED_ENTRIES_CSV)
    prepare.add_argument("--rebuild-browser", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            print(
                json.dumps(
                    eligibility_status(
                        args.people_csv,
                        args.limit,
                        args.target_manifest,
                        args.removed_csv,
                    ),
                    indent=2,
                )
            )
        elif args.command == "select":
            run_dir = create_cohort(
                people_csv=args.people_csv,
                cache_root=args.cache_root,
                batch_size=(
                    None
                    if args.all_eligible
                    else (args.batch_size if args.batch_size is not None else 60)
                ),
                run_dir=args.run_dir,
                target_manifest=args.target_manifest,
                removed_csv=args.removed_csv,
            )
            print(run_dir)
        elif args.command == "ambiguous-status":
            print(
                json.dumps(
                    ambiguous_member_status(
                        people_csv=args.people_csv,
                        state_path=args.state
                        or args.cache_root / "ambiguous-members-state.json",
                        limit=args.limit,
                        rescan_all=args.rescan_all,
                    ),
                    indent=2,
                )
            )
        elif args.command == "select-ambiguous":
            print(
                create_ambiguous_cohort(
                    people_csv=args.people_csv,
                    cache_root=args.cache_root,
                    state_path=args.state
                    or args.cache_root / "ambiguous-members-state.json",
                    batch_size=args.batch_size,
                    run_dir=args.run_dir,
                    rescan_all=args.rescan_all,
                    target_manifest=args.target_manifest,
                    review_csv=args.review_csv,
                )
            )
        elif args.command == "prepare-ambiguous-repair":
            print(
                json.dumps(
                    prepare_ambiguous_repair_manifest(
                        people_csv=args.people_csv,
                        review_csv=args.review_csv,
                        state_path=args.state
                        or args.cache_root / "ambiguous-members-state.json",
                        output=args.output,
                        action=args.action,
                        expected_count=args.expected_count,
                    ),
                    indent=2,
                )
            )
        elif args.command == "fetch":
            print(
                json.dumps(
                    fetch_articles(
                        cohort_value=args.cohort,
                        cache_root=args.cache_root,
                        max_cache_age_hours=args.max_cache_age_hours,
                    ),
                    indent=2,
                )
            )
        elif args.command == "packetize":
            print(
                json.dumps(
                    packetize_articles(
                        cohort_value=args.cohort, cache_root=args.cache_root
                    ),
                    indent=2,
                )
            )
        elif args.command == "scan-ambiguous":
            print(
                json.dumps(
                    scan_ambiguous_members(
                        cohort_value=args.cohort, cache_root=args.cache_root
                    ),
                    indent=2,
                )
            )
        elif args.command == "reuse-ambiguous-reviews":
            print(
                json.dumps(
                    reuse_ambiguous_reviews(
                        cohort_value=args.cohort,
                        state_path=args.state
                        or args.cache_root / "ambiguous-members-state.json",
                        source_cohort=args.from_current_policy_cohort,
                    ),
                    indent=2,
                )
            )
        elif args.command == "ambiguous-role-input":
            print(
                build_ambiguous_role_input(
                    cohort_value=args.cohort, qid=args.qid, output=args.output
                )
            )
        elif args.command == "ambiguous-scheduler-status":
            print(
                json.dumps(
                    ambiguous_scheduler_status(cohort_value=args.cohort), indent=2
                )
            )
        elif args.command == "claim-ambiguous-assignment":
            print(
                json.dumps(
                    claim_ambiguous_assignment(
                        cohort_value=args.cohort, slot=args.slot
                    ),
                    indent=2,
                )
            )
        elif args.command == "complete-ambiguous-assignment":
            print(
                json.dumps(
                    complete_ambiguous_assignment(
                        cohort_value=args.cohort,
                        assignment_id=args.assignment_id,
                        input_path=args.input,
                        failed_reason=args.failed_reason or "",
                        recorded_input_tokens=args.recorded_input_tokens,
                        recorded_output_tokens=args.recorded_output_tokens,
                    ),
                    indent=2,
                )
            )
        elif args.command == "finalize-ambiguous-tranche":
            print(
                json.dumps(
                    finalize_ambiguous_tranche(
                        cohort_value=args.cohort,
                        tranche=args.tranche,
                        review_csv=args.review_csv,
                        state_path=args.state,
                    ),
                    indent=2,
                )
            )
        elif args.command == "apply-ambiguous-recommendations":
            print(
                json.dumps(
                    apply_ambiguous_recommendations(
                        review_csv=args.review_csv,
                        people_csv=args.people_csv,
                        musicians_csv=args.musicians_csv,
                        removed_csv=args.removed_csv,
                    ),
                    indent=2,
                )
            )
        elif args.command in {"verify-ambiguous-repair", "publish-ambiguous-repair"}:
            repair_kwargs = {
                "cohort_value": args.cohort,
                "manifest_path": args.manifest,
                "original_review_csv": args.original_review_csv,
                "original_state_path": args.original_state
                or args.cache_root / "ambiguous-members-state.json",
                "staged_review_csv": args.staged_review_csv,
                "staged_state_path": args.staged_state,
            }
            result = (
                publish_ambiguous_repair(**repair_kwargs)
                if args.command == "publish-ambiguous-repair"
                else verify_ambiguous_repair(**repair_kwargs)
            )
            print(json.dumps(result, indent=2))
        elif args.command == "fetch-alternates":
            print(
                json.dumps(
                    fetch_alternate_articles(
                        cohort_value=args.cohort,
                        proposals_path=args.proposals,
                        cache_root=args.cache_root,
                    ),
                    indent=2,
                )
            )
        elif args.command == "init-proposals":
            print(init_proposals(cohort_value=args.cohort, output=args.output))
        elif args.command == "record-artifact":
            print(
                record_semantic_artifact(
                    cohort_value=args.cohort,
                    role=args.role,
                    qid=args.qid,
                    input_path=args.input,
                )
            )
        elif args.command == "mark-task":
            print(
                mark_semantic_task(
                    cohort_value=args.cohort,
                    role=args.role,
                    qid=args.qid,
                    status=args.status,
                    reason=args.reason,
                )
            )
        elif args.command == "role-input":
            print(
                build_role_input(
                    cohort_value=args.cohort,
                    role=args.role,
                    qid=args.qid,
                    output=args.output,
                    vocabulary_path=args.vocabulary
                    or args.cache_root / "approved-vocabulary.json",
                )
            )
        elif args.command == "aggregate-eligibility":
            print(
                json.dumps(
                    aggregate_eligibility(
                        cohort_value=args.cohort, ready_only=args.ready_only
                    ),
                    indent=2,
                )
            )
        elif args.command == "stage-status":
            print(json.dumps(stage_status(cohort_value=args.cohort), indent=2))
        elif args.command == "scheduler-status":
            print(json.dumps(scheduler_status(cohort_value=args.cohort), indent=2))
        elif args.command == "claim-assignment":
            print(
                json.dumps(
                    claim_assignment(
                        cohort_value=args.cohort,
                        slot=args.slot,
                        vocabulary_path=args.vocabulary
                        or args.cache_root / "approved-vocabulary.json",
                    ),
                    indent=2,
                )
            )
        elif args.command == "complete-assignment":
            print(
                json.dumps(
                    complete_assignment(
                        cohort_value=args.cohort,
                        assignment_id=args.assignment_id,
                        input_path=args.input,
                        failed_reason=args.failed_reason or "",
                        recorded_input_tokens=args.recorded_input_tokens,
                        recorded_output_tokens=args.recorded_output_tokens,
                    ),
                    indent=2,
                )
            )
        elif args.command == "resolve-exception":
            print(
                json.dumps(
                    resolve_exception(
                        cohort_value=args.cohort,
                        role=args.role,
                        key=args.key,
                        reason=args.reason,
                    ),
                    indent=2,
                )
            )
        elif args.command == "assemble":
            print(
                json.dumps(
                    assemble_semantic_proposals(
                        cohort_value=args.cohort,
                        output=args.output,
                        tranche=args.tranche,
                    ),
                    indent=2,
                )
            )
        elif args.command == "resolve-known":
            vocabulary = args.vocabulary or args.cache_root / "approved-vocabulary.json"
            unresolved = resolve_known_vocabulary(
                proposals_path=args.proposals,
                people_csv=args.people_csv,
                vocabulary_path=vocabulary,
                trusted_vocabulary_path=args.trusted_vocabulary,
            )
            print(json.dumps({"unresolved": unresolved}, indent=2))
        elif args.command == "lookup-vocabulary":
            print(
                json.dumps(
                    lookup_vocabulary_candidates(
                        proposals_path=args.proposals,
                        cache_root=args.cache_root,
                        max_cache_age_hours=args.max_cache_age_hours,
                        limit=args.limit,
                    ),
                    indent=2,
                )
            )
        elif args.command == "record-vocabulary":
            print(
                record_vocabulary_artifact(
                    cohort_value=args.cohort, input_path=args.input
                )
            )
        elif args.command == "vocabulary-input":
            print(
                build_vocabulary_input(
                    proposals_path=args.proposals,
                    field=args.field,
                    label=args.label,
                    output=args.output,
                )
            )
        elif args.command == "apply-vocabulary":
            vocabulary = args.vocabulary or args.cache_root / "approved-vocabulary.json"
            print(
                json.dumps(
                    apply_vocabulary_artifacts(
                        cohort_value=args.cohort,
                        proposals_path=args.proposals,
                        vocabulary_path=vocabulary,
                    ),
                    indent=2,
                )
            )
        elif args.command == "validate":
            expected_qids = None
            if args.tranche is not None:
                _, cohort = cohort_paths(args.cohort)
                tranche_qids = {
                    person["wikidata_id"]
                    for person in cohort["selected"]
                    if int(person.get("approval_tranche", 1)) == args.tranche
                }
                proposal_qids = [
                    proposal["wikidata_id"]
                    for proposal in load_proposals(args.proposals)
                ]
                if not set(proposal_qids).issubset(tranche_qids):
                    raise BatchError("Proposal QIDs are outside the requested tranche")
                expected_qids = proposal_qids
            print(
                json.dumps(
                    validate_proposals(
                        cohort_value=args.cohort,
                        proposals_path=args.proposals,
                        people_csv=args.people_csv,
                        cache_root=args.cache_root,
                        expected_qids=expected_qids,
                    ),
                    indent=2,
                )
            )
        elif args.command == "apply":
            print(
                json.dumps(
                    apply_proposals(
                        cohort_value=args.cohort,
                        proposals_path=args.proposals,
                        people_csv=args.people_csv,
                        review_csv=args.review_csv,
                        cache_root=args.cache_root,
                        tranche=args.tranche,
                    ),
                    indent=2,
                )
            )
        elif args.command == "prepare-tranche-review":
            print(
                json.dumps(
                    prepare_tranche_review(
                        cohort_value=args.cohort,
                        tranche=args.tranche,
                        proposals_path=args.proposals,
                        staged_people_csv=args.staged_people_csv,
                        staged_review_csv=args.staged_review_csv,
                    ),
                    indent=2,
                )
            )
        elif args.command == "prepare-exception-review":
            print(
                json.dumps(
                    prepare_exception_review(
                        cohort_value=args.cohort,
                        tranche=args.tranche,
                        candidate_proposals=args.candidate_proposals,
                        staged_people_csv=args.staged_people_csv,
                        output=args.output,
                    ),
                    indent=2,
                )
            )
        elif args.command == "record-tranche-approval":
            print(
                record_tranche_approval(
                    review_manifest=args.review_manifest,
                    reviewed_hash=args.reviewed_hash,
                )
            )
        elif args.command == "queue-review-correction":
            print(
                json.dumps(
                    queue_review_correction(
                        cohort_value=args.cohort,
                        review_manifest=args.review_manifest,
                        qid=args.qid,
                        reason=args.reason,
                    ),
                    indent=2,
                )
            )
        elif args.command == "migrate":
            print(
                json.dumps(
                    migrate_approved_cohort(
                        cohort_value=args.cohort,
                        proposals_path=args.proposals,
                        staged_people_csv=args.staged_people_csv,
                        staged_review_csv=args.staged_review_csv,
                        live_people_csv=args.live_people_csv,
                        live_review_csv=args.live_review_csv,
                        removed_csv=args.removed_csv,
                        cache_root=args.cache_root,
                        approval_path=args.approval,
                    ),
                    indent=2,
                )
            )
        elif args.command == "reconcile-removals":
            print(
                json.dumps(
                    reconcile_legacy_removals(
                        proposal_paths=args.proposals,
                        live_people_csv=args.live_people_csv,
                        live_review_csv=args.live_review_csv,
                        removed_csv=args.removed_csv,
                    ),
                    indent=2,
                )
            )
        elif args.command == "verify":
            expected_qids = None
            if args.tranche is not None:
                _, cohort = cohort_paths(args.cohort)
                exceptions = _scheduler_exceptions(cohort_paths(args.cohort)[0])
                exception_qids = _exception_qids(exceptions)
                expected_qids = [
                    person["wikidata_id"]
                    for person in cohort["selected"]
                    if int(person.get("approval_tranche", 1)) == args.tranche
                    and person["wikidata_id"] not in exception_qids
                ]
            print(
                json.dumps(
                    verify_batch(
                        cohort_value=args.cohort,
                        people_csv=args.people_csv,
                        musicians_csv=args.musicians_csv,
                        review_csv=args.review_csv,
                        rebuild_browser=args.rebuild_browser,
                        run_tests=args.run_tests,
                        removed_csv=args.removed_csv,
                        allow_removed=args.allow_removed,
                        expected_qids=expected_qids,
                    ),
                    indent=2,
                )
            )
        elif args.command == "prepare-redo":
            print(
                json.dumps(
                    prepare_redo(
                        people_csv=args.people_csv,
                        review_csv=args.review_csv,
                        backup_dir=args.backup_dir,
                        expected_count=args.expected_count,
                        rebuild_browser=args.rebuild_browser,
                        removed_csv=args.removed_csv,
                    ),
                    indent=2,
                )
            )
        return 0
    except BatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
