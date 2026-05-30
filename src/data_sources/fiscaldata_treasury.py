"""U.S. Treasury FiscalData API client — no API key required.

Public API at https://api.fiscaldata.treasury.gov/services/api/fiscal_service.
Spec nuances honoured:
- Dataset endpoint paths can change (the API is versioned but endpoints evolve);
  any 404 or schema mismatch degrades gracefully to None rather than raising.
- SHA-256-keyed on-disk JSON cache with TTL from config.cache_ttl_hours().
- Offline mode (config.is_offline()): cache-only; if no cache -> log_missing_data + None.
- HTTP/network errors -> log_api_failure + cache fallback + None; NEVER raises to caller.
- auction_demand_proxy(): bid-to-cover ratio history used as a weak demand signal.
  Low bid-to-cover => weak demand => positive "tail proxy" (higher yield than expected).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .. import config
from ..utils.logging import get_logger, log_api_failure, log_missing_data
from ..utils.validation import latest_zscore

_BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
_REQUEST_TIMEOUT = 15  # seconds, as per spec


class FiscalDataClient:
    """Fetches U.S. Treasury FiscalData with local JSON cache and graceful degradation.

    Parameters
    ----------
    cache_dir:
        Directory for on-disk cache.  Defaults to config.cache_dir().
    ttl_hours:
        Cache TTL in hours.  Defaults to config.cache_ttl_hours().
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_hours: float | None = None,
    ) -> None:
        self._cache_dir: Path = cache_dir if cache_dir is not None else config.cache_dir()
        self._ttl_hours: float = ttl_hours if ttl_hours is not None else config.cache_ttl_hours()
        self._logger = get_logger("ai_dc_model.fiscaldata_treasury")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self._cache_dir / f"fiscaldata_{key}.cache"

    def _is_cache_valid(self, path: Path) -> bool:
        if not path.exists():
            return False
        age_seconds = time.time() - path.stat().st_mtime
        return age_seconds < self._ttl_hours * 3600

    def _read_cache(self, path: Path) -> dict | None:
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text)
        except Exception:
            return None

    def _write_cache(self, path: Path, data: dict) -> None:
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:
            self._logger.debug("Could not write cache %s: %s", path, exc)

    def _build_url(self, path: str, params: dict | None = None) -> str:
        """Build full URL with optional query string for cache-key purposes."""
        base = _BASE_URL.rstrip("/") + "/" + path.lstrip("/")
        if not params:
            return base
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{base}?{qs}"

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """GET base+path with caching, offline mode, and graceful degradation.

        Returns
        -------
        dict | None
            Parsed JSON dict on success or cache hit.  None if unavailable.
            Never raises to caller.
        """
        url = self._build_url(path, params)
        cache_path = self._cache_path(url)

        # --- Offline mode: cache only ---
        if config.is_offline():
            if self._is_cache_valid(cache_path):
                cached = self._read_cache(cache_path)
                if cached is not None:
                    return cached
            log_missing_data(
                self._logger,
                what=f"FiscalData {path} (offline, no valid cache)",
                fallback=None,
            )
            return None

        # --- Valid cache hit ---
        if self._is_cache_valid(cache_path):
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached

        # --- Live fetch ---
        try:
            resp = requests.get(
                _BASE_URL.rstrip("/") + "/" + path.lstrip("/"),
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            # 404 degrades gracefully (endpoint path may have changed)
            if resp.status_code == 404:
                self._logger.warning(
                    "FISCALDATA_404 | path=%s | endpoint may have changed; degrading to None",
                    path,
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            self._write_cache(cache_path, data)
            return data
        except requests.HTTPError as exc:
            log_api_failure(
                self._logger,
                source="FiscalData",
                detail=f"HTTP {exc.response.status_code} for {path}",
            )
        except Exception as exc:
            log_api_failure(
                self._logger,
                source="FiscalData",
                detail=f"Request error for {path}: {exc}",
            )

        # --- Stale cache fallback ---
        stale = self._read_cache(cache_path)
        if stale is not None:
            self._logger.warning(
                "STALE_CACHE | FiscalData %s | using cached data beyond TTL",
                path,
            )
            return stale

        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def auctions(self, *, page_size: int = 100) -> pd.DataFrame | None:
        """Fetch recent Treasury auction results.

        Endpoint: /v1/accounting/od/auctions_query
        Note: this endpoint path is known to change in the FiscalData API;
        a 404 or missing 'data' key degrades to None without raising.

        Returns
        -------
        pd.DataFrame | None
            Columns (if present in API response): auction_date, security_type,
            security_term, high_yield, bid_to_cover_ratio, offering_amt,
            total_accepted.  Numeric columns are coerced; None if unavailable.
        """
        path = "/v1/accounting/od/auctions_query"
        params: dict[str, Any] = {
            "page[size]": page_size,
            "sort": "-auction_date",
        }
        raw = self._get(path, params)
        if raw is None:
            return None

        records = raw.get("data")
        if not records:
            log_missing_data(
                self._logger,
                what="FiscalData auctions data key",
                fallback=None,
            )
            return None

        try:
            df = pd.DataFrame(records)
        except Exception as exc:
            log_api_failure(
                self._logger,
                source="FiscalData",
                detail=f"DataFrame construction from auctions failed: {exc}",
                fallback="None",
            )
            return None

        # Keep only the columns we care about (if present)
        desired_cols = [
            "auction_date",
            "security_type",
            "security_term",
            "high_yield",
            "bid_to_cover_ratio",
            "offering_amt",
            "total_accepted",
        ]
        present_cols = [c for c in desired_cols if c in df.columns]
        df = df[present_cols].copy()

        # Coerce numeric columns
        numeric_cols = [
            "high_yield",
            "bid_to_cover_ratio",
            "offering_amt",
            "total_accepted",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Parse auction_date if present
        if "auction_date" in df.columns:
            df["auction_date"] = pd.to_datetime(df["auction_date"], errors="coerce")

        return df

    def auction_demand_proxy(self) -> dict | None:
        """Bid-to-cover ratio history as a weak Treasury demand signal.

        Low bid-to-cover => weak demand => bond sells at higher yield than
        expected ("positive tail").  The tail proxy is -latest_zscore(history)
        so positive values indicate weak demand / wider tail.

        Returns
        -------
        dict | None
            {
              'latest_bid_to_cover': float,
              'bid_to_cover_zscore': float,
              'auction_tail_proxy': float,   # -zscore; positive = weak demand
            }
            None if auction data is unavailable or bid_to_cover_ratio absent.

        Notes
        -----
        This is a weak/indirect proxy — bid-to-cover reflects indirect demand
        but does not capture primary dealer support, foreign official buying, or
        Fed reinvestments directly.  Treat as LOW-CONFIDENCE indicator.
        """
        df = self.auctions(page_size=200)
        if df is None:
            return None

        if "bid_to_cover_ratio" not in df.columns:
            log_missing_data(
                self._logger,
                what="bid_to_cover_ratio column in auctions DataFrame",
                fallback=None,
            )
            return None

        history_series = df["bid_to_cover_ratio"].dropna()
        if history_series.empty:
            log_missing_data(
                self._logger,
                what="bid_to_cover_ratio non-null values",
                fallback=None,
            )
            return None

        history = history_series.tolist()
        latest_btc = float(history[-1]) if history else float("nan")

        # latest_zscore uses the last element vs the whole series
        btc_zscore = latest_zscore(history)
        # Weak demand = low bid-to-cover = negative zscore -> positive tail proxy
        tail_proxy = -btc_zscore

        return {
            "latest_bid_to_cover": latest_btc,
            "bid_to_cover_zscore": btc_zscore,
            "auction_tail_proxy": tail_proxy,
        }

    def debt_outstanding(self) -> float | None:
        """Best-effort fetch of total U.S. debt outstanding (in billions USD).

        Uses the FiscalData 'debt_to_penny' dataset.  May return None if the
        endpoint is unavailable or the path has changed.

        Returns
        -------
        float | None
            Total public debt outstanding in billions USD, or None.
        """
        # Endpoint: /v2/accounting/od/debt_to_penny
        path = "/v2/accounting/od/debt_to_penny"
        params: dict[str, Any] = {
            "page[size]": 1,
            "sort": "-record_date",
        }
        raw = self._get(path, params)
        if raw is None:
            return None

        records = raw.get("data")
        if not records:
            log_missing_data(
                self._logger,
                what="FiscalData debt_to_penny data",
                fallback=None,
            )
            return None

        try:
            # 'tot_pub_debt_out_amt' is the total public debt outstanding in dollars
            row = records[0]
            raw_val = row.get("tot_pub_debt_out_amt")
            if raw_val is None:
                return None
            # Convert from dollars to billions
            return float(str(raw_val).replace(",", "")) / 1e9
        except Exception as exc:
            log_api_failure(
                self._logger,
                source="FiscalData",
                detail=f"Parsing debt_to_penny record failed: {exc}",
                fallback="None",
            )
            return None

    def get_treasury_supply_inputs(self) -> dict[str, float | None]:
        """Return Treasury auction supply/demand inputs for downstream modules.

        Robust: always returns a dict with the expected keys; values may be None
        if data is unavailable.

        Returns
        -------
        dict with keys:
            auction_tail_proxy : float | None
                Positive => weak demand / wider-than-expected auction tail.
            latest_bid_to_cover : float | None
                Most recent bid-to-cover ratio across all auction types.
        """
        proxy = self.auction_demand_proxy()
        if proxy is not None:
            return {
                "auction_tail_proxy": proxy.get("auction_tail_proxy"),
                "latest_bid_to_cover": proxy.get("latest_bid_to_cover"),
            }
        return {
            "auction_tail_proxy": None,
            "latest_bid_to_cover": None,
        }
