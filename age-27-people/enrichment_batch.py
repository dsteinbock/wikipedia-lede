#!/usr/bin/env python3
"""Deterministic mechanics for Wikipedia fallback enrichment batches.

The helper deliberately does not infer semantic facts.  It freezes cohorts,
retrieves/cache-validates article source, prepares complete semantic packets,
validates LLM-authored proposals, applies them atomically, and checks invariants.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
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
BROWSER_BUILDER = REPO_ROOT / "age-27-browser" / "build_data.py"
BROWSER_DATA = REPO_ROOT / "age-27-browser" / "data.js"
CACHE_ROOT = PROJECT_DIR / ".cache" / "wikipedia-fallback"
REVIEW_CSV = PROJECT_DIR / "wikipedia_stronger_model_review.csv"
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
STATUSES = {"settled", "provisional", "disputed", "unknown", "possible_removal"}
REMOVAL_REASONS = {
    "living",
    "nonhuman",
    "age_outside_26_28",
    "no_dedicated_person_article",
    "subject_identity_mismatch",
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
SPECIAL_VALUES = {"somevalue", "novalue"}
QID_RE = re.compile(r"Q[1-9][0-9]*")
DATE_RE = re.compile(r"^([+-]?\d+)(?:-(\d{2}))?(?:-(\d{2}))?$")
SOURCE_TIERS = {
    "lead_sentence",
    "rest_of_lead_paragraph",
    "infobox",
    "rest_of_article",
    "none",
}
DEATH_REVIEW_TIERS = [
    "lead_sentence",
    "rest_of_lead_paragraph",
    "infobox",
    "rest_of_article",
]
UNKNOWN_DISPOSITIONS = {
    "not_about_subject",
    "does_not_establish_field",
    "explicitly_unknown",
    "unconfirmed_without_usable_account",
}
DEATH_HEADING_RE = re.compile(
    r"\b(?:death|murder|killing|assassination|shooting|execution|accident|"
    r"illness|disappearance|later life|personal life)\b",
    flags=re.I,
)
DEATH_SIGNAL_RE = re.compile(
    r"\b(?:died|death|dead|killed|murdered|assassinated|suicide|overdose|"
    r"accident|crash|collision|shot|gunshot|stabbed|drowned|drowning|cancer|"
    r"lymphoma|leukemia|heart attack|cardiac|pneumonia|illness|injuries|"
    r"aneurysm|infection|stroke|explosion|airstrike|electrocuted|execution|"
    r"hanging|strangulation|suffocation|cause)\b",
    flags=re.I,
)
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


def split_values(value: object) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def usable_wikidata(value: object) -> bool:
    return any(part.casefold() not in SPECIAL_VALUES for part in split_values(value))


def field_is_effective(row: Mapping[str, str], field: str) -> bool:
    base, fallback, _ = FIELD_SPECS[field]
    return usable_wikidata(row.get(base, "")) or bool(str(row.get(fallback, "")).strip())


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
    batch_size: int,
    target_qids: Sequence[str] | None = None,
) -> tuple[list[dict[str, str]], int]:
    if batch_size <= 0:
        raise BatchError("Batch size must be positive")
    by_qid = {row["wikidata_id"]: row for row in rows}
    if target_qids is not None:
        missing = [qid for qid in target_qids if qid not in by_qid]
        if missing:
            raise BatchError(f"Target QIDs missing from people CSV: {missing}")
        eligible = [by_qid[qid] for qid in target_qids if not row_is_terminal(by_qid[qid])]
        return eligible[:batch_size], len(eligible)
    eligible = [row for row in rows if not row_is_terminal(row)]
    eligible.sort(key=lambda row: row["wikidata_id"])
    eligible.sort(
        key=lambda row: parse_latest_possible_date(row["death_date"]), reverse=True
    )
    return eligible[:batch_size], len(eligible)


def eligibility_status(
    people_csv: Path, limit: int = 3, target_manifest: Path | None = None
) -> dict[str, Any]:
    _, rows = read_csv(people_csv)
    manifest = load_target_manifest(target_manifest) if target_manifest else None
    target_qids = (
        [item["wikidata_id"] for item in manifest["people"]] if manifest else None
    )
    selected, eligible_count = select_eligible(rows, max(limit, 1), target_qids)
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
) -> dict[str, Any]:
    if expected_count <= 0:
        raise BatchError("Expected redo count must be positive")
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise BatchError(f"Backup directory is not empty: {backup_dir}")
    fieldnames, rows = read_csv(people_csv)
    target_rows = [
        row for row in rows if any(str(row.get(column, "")).strip() for column in FALLBACK_COLUMNS)
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
    globally_selected, _ = select_eligible(cleared_rows, expected_count)
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
) -> Path:
    run_started_utc = utc_now()
    fieldnames, rows = read_csv(people_csv)
    manifest = load_target_manifest(target_manifest) if target_manifest else None
    target_qids = (
        [item["wikidata_id"] for item in manifest["people"]] if manifest else None
    )
    selected, eligible_count = select_eligible(rows, batch_size, target_qids)
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
    for row in selected:
        selected_records.append(
            {
                "wikidata_id": row["wikidata_id"],
                "name": row["name"],
                "wikipedia_url": row["wikipedia_url"],
                "death_date": row["death_date"],
                "needs": {
                    field: not field_is_effective(row, field)
                    for field in FIELD_SPECS
                },
            }
        )
    cohort = {
        "schema_version": 1,
        "run_started_utc": run_started_utc,
        "requested_batch_size": batch_size,
        "eligible_count_at_selection": eligible_count,
        "selected_count": len(selected_records),
        "cohort_hash": cohort_hash,
        "target_manifest": str(target_manifest) if target_manifest else None,
        "target_manifest_sha256": (
            hashlib.sha256(target_manifest.read_bytes()).hexdigest()
            if target_manifest
            else None
        ),
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
    if cohort.get("schema_version") != 1:
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


def _subject_terms(name: str) -> list[str]:
    words = re.findall(r"[^\W\d_]+", name.casefold(), flags=re.UNICODE)
    terms = [name.casefold()]
    if words:
        terms.append(words[-1])
    return list(dict.fromkeys(term for term in terms if len(term) >= 4))


def _candidate_excerpt(text: str, match: re.Match[str], limit: int = 700) -> str:
    start = max(0, match.start() - 220)
    end = min(len(text), match.end() + 460)
    excerpt = text[start:end].strip()
    if start:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt += "…"
    return excerpt[:limit]


def _death_evidence_candidates(
    *,
    name: str,
    lead_sentence: str,
    rest_lead: str,
    infobox: str,
    sections: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    subject_terms = _subject_terms(name)

    def consider(source_tier: str, section: str, text: str, subject_required: bool) -> None:
        lowered = text.casefold()
        if subject_required and not any(term in lowered for term in subject_terms):
            return
        match = DEATH_SIGNAL_RE.search(text)
        if not match:
            return
        candidates.append(
            {
                "candidate_id": f"D{len(candidates) + 1:03d}",
                "source_tier": source_tier,
                "section": section,
                "excerpt": _candidate_excerpt(text, match),
            }
        )

    consider("lead_sentence", "Lead sentence", lead_sentence, False)
    consider("rest_of_lead_paragraph", "Lead paragraph", rest_lead, False)
    consider("infobox", "Infobox", infobox, False)
    for section in sections:
        heading = str(section["heading"])
        consider(
            "rest_of_article",
            heading,
            str(section["text"]),
            not bool(DEATH_HEADING_RE.search(heading)),
        )
    return candidates


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
    rest_article = " ".join(part for part in (remaining_lead, body_text) if part)
    infobox = clean_wikitext(infobox_raw)
    death_candidates = _death_evidence_candidates(
        name=str(article["name"]),
        lead_sentence=lead_sentence,
        rest_lead=rest_lead,
        infobox=infobox,
        sections=sections,
    )
    semantic_size = sum(
        len(value)
        for value in (lead_sentence, rest_lead, infobox, rest_article)
    )
    if not lead_sentence or semantic_size == 0:
        raise BatchError(f"Could not packetize {article['wikidata_id']}")
    return {
        "schema_version": 2,
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
        "infobox": infobox,
        "rest_of_article": rest_article,
        "article_sections": sections,
        "death_evidence_candidates": death_candidates,
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
    proposals_path: Path,
    cache_root: Path,
    batch_limit: int = 20,
) -> dict[str, int]:
    """Fetch every non-English sitelink for primary pages rejected as non-person pages."""
    run_dir, cohort = cohort_paths(cohort_value)
    selected = {person["wikidata_id"]: person for person in cohort["selected"]}
    proposals = {item["wikidata_id"]: item for item in load_proposals(proposals_path)}
    requested: list[str] = []
    for qid in selected:
        review = proposals.get(qid, {}).get("article_eligibility", {})
        primary = review.get("primary_review", {}) if isinstance(review, dict) else {}
        if primary.get("page_kind") in PAGE_KINDS - {"person"}:
            requested.append(qid)
    if not requested:
        raise BatchError("No proposals request non-English article review")

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
    index: dict[str, list[dict[str, Any]]] = {qid: [] for qid in requested}
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
    atomic_write_json(run_dir / "alternate_article_index.json", index)
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
) -> None:
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
    required = {
        "page_kind",
        "subject_is_human",
        "life_status",
        "age_compatibility",
        "reason",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise BatchError(f"{qid}: malformed article review for {candidate_id}")
    review = {key: str(value[key]).strip() for key in required}
    if review["page_kind"] not in PAGE_KINDS:
        raise BatchError(f"{qid}: invalid page kind for {candidate_id}")
    if review["subject_is_human"] not in HUMAN_STATUSES:
        raise BatchError(f"{qid}: invalid human status for {candidate_id}")
    if review["life_status"] not in LIFE_STATUSES:
        raise BatchError(f"{qid}: invalid life status for {candidate_id}")
    if review["age_compatibility"] not in AGE_COMPATIBILITIES:
        raise BatchError(f"{qid}: invalid age compatibility for {candidate_id}")
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
    if not isinstance(value, dict) or set(value) != required:
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
    selected_article = str(value["selected_article"]).strip()
    if decision == "eligible":
        if removal_reasons:
            raise BatchError(f"{qid}: eligible article cannot have removal reasons")
        if selected_article not in reviewed:
            raise BatchError(f"{qid}: selected article was not reviewed")
        chosen = reviewed[selected_article]
        if chosen["page_kind"] != "person" or chosen["subject_is_human"] == "nonhuman":
            raise BatchError(f"{qid}: selected article is not a human-person page")
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
                and review["subject_is_human"] != "nonhuman"
                and review["life_status"] not in {"living", "conflicting"}
                and review["age_compatibility"]
                not in {"outside_26_28", "conflicting"}
            ]
            largest = max(
                qualifying,
                key=lambda candidate_id: int(candidate_items[candidate_id]["article_bytes"]),
            )
            if selected_article != largest:
                raise BatchError(f"{qid}: selected alternate is not the largest qualifying article")
    else:
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
    if packet.get("schema_version") != 2:
        raise BatchError(f"{qid}: unsupported selected article packet schema")
    return dict(value), packet, article_kind


def verify_protected(
    cohort: Mapping[str, Any], fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> None:
    if list(fieldnames) != list(cohort["public_csv_columns"]):
        raise BatchError("Public people CSV schema changed after cohort selection")
    by_qid = {row["wikidata_id"]: row for row in rows}
    current = {}
    for qid, snapshot in cohort["protected_rows"].items():
        if qid not in by_qid:
            raise BatchError(f"Selected QID disappeared: {qid}")
        current[qid] = {
            column: by_qid[qid].get(column, "")
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
) -> dict[str, Any]:
    run_dir, cohort = cohort_paths(cohort_value)
    fieldnames, rows = read_csv(people_csv)
    verify_protected(cohort, fieldnames, rows)
    rows_by_qid = {row["wikidata_id"]: row for row in rows}
    proposals = load_proposals(proposals_path)
    selected = {person["wikidata_id"]: person for person in cohort["selected"]}
    proposal_qids = [str(proposal.get("wikidata_id", "")) for proposal in proposals]
    if len(proposal_qids) != len(set(proposal_qids)):
        raise BatchError("Duplicate proposal QID")
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
        if proposal.get("status") not in STATUSES:
            raise BatchError(f"{qid}: invalid/blank status")
        status_counts[proposal["status"]] += 1
        evidence = proposal.get("evidence_basis")
        if not isinstance(evidence, dict) or set(evidence) != set(FIELD_SPECS):
            raise BatchError(f"{qid}: malformed field-specific evidence basis")
        unknown_review = proposal.get("unknown_review")
        if not isinstance(unknown_review, dict) or set(unknown_review) != {
            "cause",
            "manner",
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
                )
                if pairs[0]["label"] == "somevalue":
                    somevalue_counts[field] += 1
                    if field in {"cause", "manner"}:
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

        death_unknown = effective_unknown("cause") or effective_unknown("manner")
        if proposal["status"] == "unknown" and not death_unknown:
            raise BatchError(f"{qid}: unknown status has no unknown death field")
        if proposal["status"] == "settled" and death_unknown:
            raise BatchError(f"{qid}: settled status conflicts with unknown death field")
    return {
        "selected": len(selected),
        "validated": len(proposals),
        "status_counts": status_counts,
        "somevalue_counts": somevalue_counts,
        "audited_unknown_fields": audited_unknown_fields,
    }


def _seed_vocabulary(
    people_csv: Path, vocabulary_path: Path
) -> dict[str, dict[str, str]]:
    _, rows = read_csv(people_csv)
    mappings: dict[str, dict[str, str]] = {}

    def add(label: str, qid: str) -> None:
        key = label.casefold()
        existing = mappings.get(key)
        if existing and existing["qid"] != qid:
            raise BatchError(
                f"Conflicting vocabulary mapping for {label!r}: "
                f"{existing['qid']} vs {qid}"
            )
        mappings[key] = {"label": label, "qid": qid}

    for row in rows:
        for _, label_column, qid_column in FIELD_SPECS.values():
            labels = split_values(row.get(label_column, ""))
            qids = split_values(row.get(qid_column, ""))
            if labels == ["somevalue"]:
                continue
            if len(labels) == len(qids):
                for label, qid in zip(labels, qids):
                    if QID_RE.fullmatch(qid):
                        add(label, qid)
    if vocabulary_path.exists():
        payload = json.loads(vocabulary_path.read_text(encoding="utf-8"))
        for value in payload.get("mappings", {}).values():
            if QID_RE.fullmatch(str(value.get("qid", ""))):
                add(str(value["label"]), str(value["qid"]))
    return mappings


def resolve_known_vocabulary(
    *,
    proposals_path: Path,
    people_csv: Path,
    vocabulary_path: Path,
) -> list[str]:
    proposals = load_proposals(proposals_path)
    mappings = _seed_vocabulary(people_csv, vocabulary_path)

    def add(label: str, qid: str) -> None:
        key = label.casefold()
        existing = mappings.get(key)
        if existing and existing["qid"] != qid:
            raise BatchError(
                f"Conflicting vocabulary mapping for {label!r}: "
                f"{existing['qid']} vs {qid}"
            )
        mappings[key] = {"label": label, "qid": qid}

    for proposal in proposals:
        for field in FIELD_SPECS:
            value = proposal.get(field)
            if not isinstance(value, list):
                continue
            for pair in value:
                label, qid = str(pair.get("label", "")).strip(), str(
                    pair.get("qid", "")
                ).strip()
                if label != "somevalue" and label and QID_RE.fullmatch(qid):
                    add(label, qid)
    unresolved: set[str] = set()
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
                known = mappings.get(label.casefold())
                if not str(pair.get("qid", "")).strip() and known:
                    pair["label"] = known["label"]
                    pair["qid"] = known["qid"]
                if not QID_RE.fullmatch(str(pair.get("qid", "")).strip()):
                    unresolved.add(label)
            value.sort(key=lambda pair: str(pair.get("label", "")).casefold())
    write_proposals(proposals_path, proposals)
    atomic_write_json(
        vocabulary_path,
        {
            "schema_version": 1,
            "updated_utc": utc_now(),
            "mappings": dict(sorted(mappings.items())),
        },
    )
    atomic_write_json(
        proposals_path.with_name("unresolved_vocabulary.json"), sorted(unresolved)
    )
    return sorted(unresolved)


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
        not isinstance(label, str) or not label.strip() for label in unresolved
    ):
        raise BatchError("Invalid unresolved_vocabulary.json")
    labels = sorted({label.strip() for label in unresolved}, key=str.casefold)
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
    output = {
        "schema_version": 1,
        "generated_utc": utc_now(),
        "labels": results,
    }
    atomic_write_json(proposals_path.with_name("vocabulary_candidates.json"), output)
    return {
        "unique_labels": len(labels),
        "cache_hits": cache_hits,
        "external_lookups": lookups,
    }


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
) -> dict[str, Any]:
    result = validate_proposals(
        cohort_value=cohort_value,
        proposals_path=proposals_path,
        people_csv=people_csv,
        cache_root=cache_root,
    )
    run_dir, cohort = cohort_paths(cohort_value)
    fieldnames, rows = read_csv(people_csv)
    proposals = {
        proposal["wikidata_id"]: proposal for proposal in load_proposals(proposals_path)
    }
    selected = {person["wikidata_id"]: person for person in cohort["selected"]}
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
    for person in cohort["selected"]:
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
        run_dir / "research_log.jsonl",
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in research_rows),
    )
    cohort["stages"]["staging_apply_complete_utc"] = utc_now()
    atomic_write_json(run_dir / "cohort.json", cohort)
    result["review_queue_rows"] = len(review_rows_out)
    return result


def verify_batch(
    *,
    cohort_value: Path,
    people_csv: Path,
    musicians_csv: Path,
    review_csv: Path,
    rebuild_browser: bool,
    run_tests: bool,
) -> dict[str, Any]:
    run_dir, cohort = cohort_paths(cohort_value)
    fieldnames, rows = read_csv(people_csv)
    verify_protected(cohort, fieldnames, rows)
    validate_public_rows(fieldnames, rows)
    by_qid = {row["wikidata_id"]: row for row in rows}
    incomplete = [
        person["wikidata_id"]
        for person in cohort["selected"]
        if not row_is_terminal(by_qid[person["wikidata_id"]])
    ]
    if incomplete:
        raise BatchError(f"Selected people remain incomplete: {incomplete}")

    review_fields, review_rows = read_csv(review_csv)
    if review_fields != REVIEW_COLUMNS:
        raise BatchError("Review queue schema mismatch")
    review_qids = [row["wikidata_id"] for row in review_rows]
    if len(review_qids) != len(set(review_qids)):
        raise BatchError("Duplicate review queue QID")

    _, musicians = read_csv(musicians_csv)
    missing_musicians = [
        row["wikidata_id"] for row in musicians if row["wikidata_id"] not in by_qid
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
        for test_dir in (
            PROJECT_DIR / "tests",
            REPO_ROOT / "age-27-musicians" / "tests",
            REPO_ROOT / "age-27-browser" / "tests",
        ):
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
            test_results.append(
                {"directory": str(test_dir), "tests": int(match.group(1)) if match else None}
            )
        diff_check = subprocess.run(
            ["git", "diff", "--check"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
        )
        if diff_check.returncode:
            raise BatchError(f"git diff --check failed:\n{diff_check.stdout}")

    cohort["stages"]["final_validation_complete_utc"] = utc_now()
    cohort["run_ended_utc"] = cohort["stages"]["final_validation_complete_utc"]
    atomic_write_json(run_dir / "cohort.json", cohort)
    result = {
        "selected": len(cohort["selected"]),
        "completed": len(cohort["selected"]) - len(incomplete),
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

    select = subparsers.add_parser("select", help="Freeze the next eligible cohort")
    select.add_argument("--batch-size", type=int, default=100)
    select.add_argument("--run-dir", type=Path)
    select.add_argument("--target-manifest", type=Path)

    fetch = subparsers.add_parser("fetch", help="Bulk-fetch/cache current articles")
    fetch.add_argument("--cohort", type=Path, required=True)
    fetch.add_argument("--max-cache-age-hours", type=float, default=24)

    alternates = subparsers.add_parser(
        "fetch-alternates",
        help="Fetch every non-English sitelink for rejected English pages",
    )
    alternates.add_argument("--cohort", type=Path, required=True)
    alternates.add_argument("--proposals", type=Path, required=True)

    packetize = subparsers.add_parser(
        "packetize", help="Create complete hierarchical semantic packets"
    )
    packetize.add_argument("--cohort", type=Path, required=True)

    init = subparsers.add_parser(
        "init-proposals", help="Create the strict proposal JSONL template"
    )
    init.add_argument("--cohort", type=Path, required=True)
    init.add_argument("--output", type=Path)

    resolve = subparsers.add_parser(
        "resolve-known", help="Fill proposal QIDs from established vocabulary"
    )
    resolve.add_argument("--proposals", type=Path, required=True)
    resolve.add_argument("--vocabulary", type=Path)

    lookup = subparsers.add_parser(
        "lookup-vocabulary",
        help="Cache Wikidata Search API candidates for unresolved labels",
    )
    lookup.add_argument("--proposals", type=Path, required=True)
    lookup.add_argument("--max-cache-age-hours", type=float, default=168)
    lookup.add_argument("--limit", type=int, default=8)

    validate = subparsers.add_parser(
        "validate", help="Validate staged proposals without public writes"
    )
    validate.add_argument("--cohort", type=Path, required=True)
    validate.add_argument("--proposals", type=Path, required=True)

    apply = subparsers.add_parser(
        "apply", help="Atomically apply validated proposals and merge review queue"
    )
    apply.add_argument("--cohort", type=Path, required=True)
    apply.add_argument("--proposals", type=Path, required=True)
    apply.add_argument("--review-csv", type=Path, default=REVIEW_CSV)

    verify = subparsers.add_parser(
        "verify", help="Verify effective fields, browser, tests, and invariants"
    )
    verify.add_argument("--cohort", type=Path, required=True)
    verify.add_argument("--musicians-csv", type=Path, default=MUSICIANS_CSV)
    verify.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    verify.add_argument("--rebuild-browser", action="store_true")
    verify.add_argument("--run-tests", action="store_true")

    prepare = subparsers.add_parser(
        "prepare-redo", help="Back up and clear the currently enriched rows"
    )
    prepare.add_argument("--expected-count", type=int, required=True)
    prepare.add_argument("--backup-dir", type=Path, required=True)
    prepare.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    prepare.add_argument("--rebuild-browser", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            print(
                json.dumps(
                    eligibility_status(
                        args.people_csv, args.limit, args.target_manifest
                    ),
                    indent=2,
                )
            )
        elif args.command == "select":
            run_dir = create_cohort(
                people_csv=args.people_csv,
                cache_root=args.cache_root,
                batch_size=args.batch_size,
                run_dir=args.run_dir,
                target_manifest=args.target_manifest,
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
        elif args.command == "resolve-known":
            vocabulary = args.vocabulary or args.cache_root / "vocabulary.json"
            unresolved = resolve_known_vocabulary(
                proposals_path=args.proposals,
                people_csv=args.people_csv,
                vocabulary_path=vocabulary,
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
        elif args.command == "validate":
            print(
                json.dumps(
                    validate_proposals(
                        cohort_value=args.cohort,
                        proposals_path=args.proposals,
                        people_csv=args.people_csv,
                        cache_root=args.cache_root,
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
                    ),
                    indent=2,
                )
            )
        elif args.command == "verify":
            print(
                json.dumps(
                    verify_batch(
                        cohort_value=args.cohort,
                        people_csv=args.people_csv,
                        musicians_csv=args.musicians_csv,
                        review_csv=args.review_csv,
                        rebuild_browser=args.rebuild_browser,
                        run_tests=args.run_tests,
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
