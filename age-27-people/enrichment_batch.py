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
PEOPLE_CSV = PROJECT_DIR / "age_27_people.csv"
MUSICIANS_CSV = REPO_ROOT / "age-27-musicians" / "age_27_musicians.csv"
CLUB_ARCHIVE_HTML = REPO_ROOT / "age-27-musicians" / "purported-27-club-members.html"
BROWSER_BUILDER = REPO_ROOT / "age-27-browser" / "build_data.py"
BROWSER_DATA = REPO_ROOT / "age-27-browser" / "data.js"
CACHE_ROOT = PROJECT_DIR / ".cache" / "wikipedia-fallback"
REVIEW_CSV = PROJECT_DIR / "wikipedia_stronger_model_review.csv"
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
MAX_SEMANTIC_SLOTS = 3
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
    if ready["eligibility"] and "eligibility" not in active_roles:
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
        if chosen["subject_match"] != "match" or chosen["subject_is_human"] == "nonhuman":
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
        "exceptions_excluded": sorted(exception_qids & {
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
