"""Serial, cached HTTP clients that follow Wikimedia API etiquette."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

from .core import DataValidationError


WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
GRAPHQL_ENDPOINT = "https://www.wikidata.org/w/api.php"
DEFAULT_USER_AGENT = "Age27Research/2.0 (https://github.com/dsteinbock/wikipedia-lede)"


class QueryTimeout(RuntimeError):
    """Raised when WDQS cannot complete a query within the chosen deadline."""


def _retry_delay(value: str | None, now: Callable[[], datetime]) -> float:
    if not value:
        return 0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class _CachedClient:
    def __init__(
        self,
        cache_dir: Path,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.cache_dir = cache_dir
        self.session = session or requests.Session()
        self.sleep = sleep
        self.now = now
        self.user_agent = user_agent
        self.last_request_finished = 0.0

    def _cache_path(self, request_text: str) -> Path:
        key = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _load(self, request_text: str) -> dict | None:
        path = self._cache_path(request_text)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _store(self, request_text: str, payload: dict) -> None:
        path = self._cache_path(request_text)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _pace(self) -> None:
        gap = time.monotonic() - self.last_request_finished
        if self.last_request_finished and gap < 1.0:
            self.sleep(1.0 - gap)


class WDQSClient(_CachedClient):
    def query(self, sparql: str) -> dict:
        normalized = "\n".join(line.rstrip() for line in sparql.strip().splitlines())
        cached = self._load(normalized)
        if cached is not None:
            return cached
        params = {"query": normalized, "format": "json"}
        prepared = requests.Request("GET", WDQS_ENDPOINT, params=params).prepare()
        if len(prepared.url or "") > 8000:
            raise DataValidationError("SPARQL GET URL exceeds 8,000 characters")
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/sparql-results+json",
            "Accept-Encoding": "gzip, deflate",
        }
        for attempt in range(3):
            self._pace()
            try:
                response = self.session.get(
                    WDQS_ENDPOINT, params=params, headers=headers, timeout=(10, 45)
                )
            except requests.Timeout as exc:
                self.last_request_finished = time.monotonic()
                raise QueryTimeout("WDQS query exceeded the 45-second client timeout") from exc
            except requests.RequestException:
                self.last_request_finished = time.monotonic()
                if attempt == 2:
                    raise
                self.sleep(min(5 * (2**attempt), 60))
                continue
            self.last_request_finished = time.monotonic()
            if response.status_code in {500, 504} and "timeout" in response.text[:1000].lower():
                raise QueryTimeout("WDQS reported a query timeout")
            if response.status_code in {429, 503}:
                if attempt == 2:
                    response.raise_for_status()
                delay = _retry_delay(response.headers.get("Retry-After"), self.now)
                self.sleep(delay if delay > 0 else min(5 * (2**attempt), 60))
                continue
            if response.status_code >= 500:
                if attempt == 2:
                    response.raise_for_status()
                self.sleep(min(5 * (2**attempt), 60))
                continue
            if response.status_code >= 400:
                raise DataValidationError(
                    f"WDQS HTTP {response.status_code}: {response.text[:2000]}"
                )
            response.raise_for_status()
            payload = response.json()
            self._store(normalized, payload)
            return payload
        raise AssertionError("unreachable")


class GraphQLClient(_CachedClient):
    def query(self, graphql: str) -> dict:
        normalized = "\n".join(line.rstrip() for line in graphql.strip().splitlines())
        cached = self._load(normalized)
        if cached is not None and not cached.get("error"):
            return cached
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Content-Type": "application/json",
        }
        params = {"action": "wbgraphql", "format": "json", "maxlag": "10"}
        for attempt in range(4):
            self._pace()
            try:
                response = self.session.post(
                    GRAPHQL_ENDPOINT,
                    params=params,
                    json={"query": normalized},
                    headers=headers,
                    timeout=(10, 45),
                )
            except requests.RequestException:
                self.last_request_finished = time.monotonic()
                if attempt == 3:
                    raise
                self.sleep(min(5 * (2**attempt), 60))
                continue
            self.last_request_finished = time.monotonic()
            if response.status_code in {429, 503}:
                if attempt == 3:
                    response.raise_for_status()
                delay = _retry_delay(response.headers.get("Retry-After"), self.now)
                self.sleep(delay if delay > 0 else min(5 * (2**attempt), 60))
                continue
            if response.status_code >= 500:
                if attempt == 3:
                    response.raise_for_status()
                self.sleep(min(5 * (2**attempt), 60))
                continue
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                if attempt == 3:
                    raise DataValidationError(f"GraphQL API error: {payload['error']}")
                self.sleep(min(5 * (2**attempt), 60))
                continue
            if payload.get("errors"):
                raise DataValidationError(f"GraphQL errors: {payload['errors']}")
            self._store(normalized, payload)
            return payload
        raise AssertionError("unreachable")
