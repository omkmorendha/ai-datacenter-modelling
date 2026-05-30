"""SEC EDGAR companyfacts client for the AI datacenter macro stress model.

Pulls XBRL companyfacts data from https://data.sec.gov/api/xbrl/companyfacts/
for hyperscalers and datacenter operators defined in the entity universe.

SEC fair-access policy:
  - Requires a descriptive User-Agent header (company name + contact email).
  - Rate limit: <=10 requests/second; we sleep 0.15 s before each live request.
  - See https://www.sec.gov/developer for terms.

Spec nuances honored:
  - Offline mode: cache-only; missing cache -> log_missing_data + None, no raise.
  - Cache keyed by sha256(url), TTL from config.cache_ttl_hours().
  - latest_concept_value prefers annual 10-K/FY filings over interim data.
  - Private-credit estimates are LOW-CONFIDENCE; distinguish MTM vs realized.
  - Capex is 'announced vs spent' (cash payments per statement of cash flows).
  - Lease liabilities are split operating vs finance (gross, not net of contra).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

from .. import config
from ..utils.logging import (
    get_logger,
    log_api_failure,
    log_missing_data,
)

_logger = get_logger("ai_dc_model.sec_edgar")

# Fields we extract (aligned with xbrl_concepts in entity_universe.yaml)
_DEFAULT_FIELDS = [
    "capex",
    "operating_cash_flow",
    "revenue",
    "operating_income",
    "total_debt",
    "long_term_debt",
    "short_term_debt",
    "interest_expense",
    "operating_lease_liabilities",
    "finance_lease_liabilities",
    "cash_and_equivalents",
    "marketable_securities",
]

_EDGAR_BASE = "https://data.sec.gov"


class EdgarClient:
    """HTTP client for SEC EDGAR companyfacts API with caching and rate limiting.

    This is a plain class (not a pydantic model) because it holds stateful
    HTTP session resources and mutable cache state.
    """

    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: Path | None = None,
        ttl_hours: float | None = None,
    ) -> None:
        self.user_agent: str = user_agent or config.SEC_USER_AGENT or (
            "ai-datacenter-macro-model contact@example.com"
        )
        self._cache_dir: Path = cache_dir or config.cache_dir()
        self._ttl_hours: float = ttl_hours if ttl_hours is not None else config.cache_ttl_hours()

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self._cache_dir / f"{key}.json"

    def _cache_is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age_seconds = time.time() - path.stat().st_mtime
        return age_seconds < self._ttl_hours * 3600

    def _load_cache(self, path: Path) -> dict | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save_cache(self, path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except Exception as exc:
            _logger.debug("Cache write failed for %s: %s", path, exc)

    def _get_json(self, url: str) -> dict | None:
        """Fetch JSON from URL with on-disk caching and rate limiting.

        Returns cached data if fresh; falls back to stale cache on failure.
        Returns None without raising on any error.
        """
        cache_path = self._cache_path(url)

        # Serve from fresh cache
        if self._cache_is_fresh(cache_path):
            return self._load_cache(cache_path)

        # Offline mode: try stale cache, else log and return None
        if config.is_offline():
            data = self._load_cache(cache_path)
            if data is None:
                log_missing_data(
                    _logger,
                    what=f"SEC EDGAR offline cache miss: {url}",
                    fallback=None,
                )
            return data

        # Live request: respect <=10 req/s
        time.sleep(0.15)
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data: dict = resp.json()
            self._save_cache(cache_path, data)
            return data
        except Exception as exc:
            log_api_failure(
                _logger,
                source="SEC EDGAR",
                detail=f"GET {url} failed: {exc}",
                fallback="cache/assumptions",
            )
            # Fall back to stale cache
            return self._load_cache(cache_path)

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def company_facts(self, cik: str | int | None) -> dict | None:
        """Return the full companyfacts payload for a given CIK.

        Args:
            cik: 10-digit CIK string, integer CIK, or None.

        Returns:
            Parsed JSON dict or None if unavailable.
        """
        if not cik:
            log_missing_data(
                _logger,
                what="company_facts: CIK is None/empty",
                fallback=None,
            )
            return None

        cik_padded = f"{int(cik):010d}"
        url = f"{_EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
        return self._get_json(url)

    def latest_concept_value(
        self,
        facts: dict,
        concept: str,
        unit: str = "USD",
    ) -> float | None:
        """Extract the latest value for an XBRL concept from a companyfacts dict.

        Prefers annual 10-K / FY filings over interim data. Returns None if
        the concept or unit is absent.

        Args:
            facts: companyfacts payload (from company_facts()).
            concept: US-GAAP concept name, e.g. "PaymentsToAcquirePropertyPlantAndEquipment".
            unit: unit key, default "USD".

        Returns:
            float or None.
        """
        try:
            filings: list[dict] = (
                facts["facts"]["us-gaap"][concept]["units"][unit]
            )
        except (KeyError, TypeError):
            return None

        if not filings:
            return None

        # Prefer 10-K / FY annual filings for full-year numbers
        annual = [
            f for f in filings
            if f.get("form") in ("10-K", "10-K/A") or f.get("fp") == "FY"
        ]
        candidates = annual if annual else filings

        # Pick latest by 'end' date string (ISO 8601 comparison is lexicographic)
        try:
            best = max(candidates, key=lambda f: f.get("end", ""))
            val = best.get("val")
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    def extract_financials(
        self,
        cik: str | int | None,
        concept_map: dict[str, list[str]] | None = None,
    ) -> dict[str, float | None]:
        """Extract standardised financial fields for one company by CIK.

        Uses the first matching XBRL tag in concept_map for each field.
        Missing fields are logged and returned as None.

        Args:
            cik: company CIK.
            concept_map: mapping field -> list[xbrl_tag]. Defaults to
                config.load_entity_universe()['xbrl_concepts'].

        Returns:
            dict field -> float|None for every field in _DEFAULT_FIELDS.
        """
        if concept_map is None:
            concept_map = config.load_entity_universe().get("xbrl_concepts", {})

        result: dict[str, float | None] = {f: None for f in _DEFAULT_FIELDS}

        facts = self.company_facts(cik)
        if facts is None:
            for field in _DEFAULT_FIELDS:
                log_missing_data(
                    _logger,
                    what=f"extract_financials: no facts for CIK={cik}, field={field}",
                    fallback=None,
                )
            return result

        for field in _DEFAULT_FIELDS:
            tags: list[str] = concept_map.get(field, [])
            found: float | None = None
            for tag in tags:
                val = self.latest_concept_value(facts, tag)
                if val is not None:
                    found = val
                    break
            if found is None:
                log_missing_data(
                    _logger,
                    what=(
                        f"extract_financials: field={field} not found "
                        f"(CIK={cik}, tried {tags})"
                    ),
                    fallback=None,
                )
            result[field] = found

        return result

    def fetch_universe(self) -> dict[str, dict[str, float | None]]:
        """Fetch financials for all companies in the entity universe.

        Iterates hyperscalers + datacenter_operators; skips entities with
        CIK=None (logs missing data).

        Returns:
            dict company_name -> financials_dict (field -> float|None).
        """
        universe = config.load_entity_universe()
        companies: list[dict[str, Any]] = (
            universe.get("hyperscalers", [])
            + universe.get("datacenter_operators", [])
        )

        results: dict[str, dict[str, float | None]] = {}
        for company in companies:
            name: str = company.get("name", "<unknown>")
            cik = company.get("cik")
            if not cik:
                log_missing_data(
                    _logger,
                    what=f"fetch_universe: no CIK for {name}, skipping SEC fetch",
                    fallback="assumptions only",
                )
                continue
            _logger.info("Fetching SEC financials for %s (CIK %s)", name, cik)
            results[name] = self.extract_financials(cik)

        return results
