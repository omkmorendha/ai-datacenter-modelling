"""FRED (Federal Reserve Economic Data) client with on-disk caching and graceful degradation.

Spec nuances honoured:
- Rate series (DGS*, SOFR, EFFR, FEDFUNDS, T10YIE) are stored as percent in FRED;
  get_latest_levels() and get_treasury_inputs() DIVIDE BY 100 to return decimals.
- '.' observation values from FRED JSON are treated as NaN (missing).
- Two fetch paths: JSON (requires API key) and public CSV (no key required).
- Cache keyed by sha256(url); TTL based on file mtime vs config.cache_ttl_hours().
- Offline mode (config.is_offline()): cache-only; missing cache -> log + return None.
- HTTP errors -> log_api_failure + try cache fallback + return None. NEVER raises to caller.
- get_treasury_inputs() returns the dict expected by treasury_module.build_treasury_module(fred_data=...).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .. import config
from ..utils.logging import (
    get_logger,
    log_api_failure,
    log_missing_data,
)

# ---------------------------------------------------------------------------
# Series ID constants
# ---------------------------------------------------------------------------
SOFR = "SOFR"
EFFR = "EFFR"
FEDFUNDS = "FEDFUNDS"
DGS2 = "DGS2"
DGS10 = "DGS10"
DGS30 = "DGS30"
T10YIE = "T10YIE"

# Rate series that FRED expresses as percent -> must /100 to get decimal
_RATE_SERIES: frozenset[str] = frozenset(
    {SOFR, EFFR, FEDFUNDS, DGS2, DGS10, DGS30, T10YIE}
)

_DEFAULT_SERIES_IDS: list[str] = [SOFR, EFFR, FEDFUNDS, DGS2, DGS10, DGS30, T10YIE]

_FRED_JSON_BASE = "https://api.stlouisfed.org/fred/series/observations"
_FRED_CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"

_REQUEST_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# FredClient
# ---------------------------------------------------------------------------
class FredClient:
    """Fetches FRED time-series with local JSON cache and graceful degradation.

    Parameters
    ----------
    api_key:
        FRED API key.  Defaults to config.FRED_API_KEY.  If None/empty the
        client falls back to the unauthenticated public CSV endpoint.
    cache_dir:
        Directory for on-disk cache.  Defaults to config.cache_dir().
    ttl_hours:
        Cache TTL in hours.  Defaults to config.cache_ttl_hours().
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        ttl_hours: float | None = None,
    ) -> None:
        self._api_key: str | None = api_key if api_key is not None else config.FRED_API_KEY
        self._cache_dir: Path = cache_dir if cache_dir is not None else config.cache_dir()
        self._ttl_hours: float = ttl_hours if ttl_hours is not None else config.cache_ttl_hours()
        self._logger = get_logger("ai_dc_model.fred")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self._cache_dir / f"fred_{key}.cache"

    def _is_cache_valid(self, path: Path) -> bool:
        if not path.exists():
            return False
        age_seconds = time.time() - path.stat().st_mtime
        return age_seconds < self._ttl_hours * 3600

    def _read_cache(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _write_cache(self, path: Path, content: str) -> None:
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as exc:
            self._logger.debug("Could not write cache %s: %s", path, exc)

    def _fetch_url(self, url: str) -> str | None:
        """Fetch URL text content; returns None on any error."""
        try:
            resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as exc:
            log_api_failure(
                self._logger,
                source="FRED",
                detail=f"HTTP {exc.response.status_code} for {url}",
            )
            return None
        except Exception as exc:
            log_api_failure(
                self._logger,
                source="FRED",
                detail=f"Request error for {url}: {exc}",
            )
            return None

    def _parse_json_response(self, text: str, series_id: str) -> pd.Series | None:
        """Parse FRED JSON observations -> DatetimeIndex float Series."""
        try:
            data = json.loads(text)
            obs = data.get("observations", [])
            if not obs:
                return None
            dates, values = [], []
            for o in obs:
                raw_val = o.get("value", ".")
                dates.append(o["date"])
                values.append(float("nan") if raw_val == "." else float(raw_val))
            index = pd.to_datetime(dates)
            return pd.Series(values, index=index, name=series_id, dtype=float)
        except Exception as exc:
            self._logger.debug("Failed to parse JSON for %s: %s", series_id, exc)
            return None

    def _parse_csv_response(self, text: str, series_id: str) -> pd.Series | None:
        """Parse FRED public CSV -> DatetimeIndex float Series."""
        try:
            df = pd.read_csv(
                io.StringIO(text),
                parse_dates=[0],
                index_col=0,
                na_values=[".", ""],
            )
            if df.empty:
                return None
            col = df.iloc[:, 0].astype(float)
            col.name = series_id
            col.index = pd.to_datetime(col.index)
            return col
        except Exception as exc:
            self._logger.debug("Failed to parse CSV for %s: %s", series_id, exc)
            return None

    def _load_from_cache_if_any(
        self, url: str, series_id: str, is_json: bool
    ) -> pd.Series | None:
        """Try to load stale / fallback cache regardless of TTL."""
        path = self._cache_path(url)
        text = self._read_cache(path)
        if text is None:
            return None
        if is_json:
            return self._parse_json_response(text, series_id)
        return self._parse_csv_response(text, series_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_series(
        self,
        series_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.Series | None:
        """Fetch a FRED series as a DatetimeIndex float Series.

        Returns
        -------
        pd.Series | None
            DatetimeIndex, dtype float.  NaN for missing FRED observations.
            None if data is unavailable and no cache exists.

        Notes
        -----
        Values are returned as-is from FRED (percent for rate series).
        Callers that need decimals should divide by 100 or use get_latest_levels().
        """
        use_json = bool(self._api_key)

        if use_json:
            params: dict[str, str] = {
                "series_id": series_id,
                "api_key": self._api_key,  # type: ignore[assignment]
                "file_type": "json",
            }
            if start:
                params["observation_start"] = start
            if end:
                params["observation_end"] = end
            # Build deterministic URL for cache key
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            url = f"{_FRED_JSON_BASE}?{param_str}"
        else:
            url = f"{_FRED_CSV_BASE}?id={series_id}"

        cache_path = self._cache_path(url)

        # --- Offline mode ---
        if config.is_offline():
            if self._is_cache_valid(cache_path):
                text = self._read_cache(cache_path)
                if text:
                    return (
                        self._parse_json_response(text, series_id)
                        if use_json
                        else self._parse_csv_response(text, series_id)
                    )
            log_missing_data(
                self._logger,
                what=f"FRED series {series_id} (offline, no valid cache)",
                fallback=None,
            )
            return None

        # --- Cache hit ---
        if self._is_cache_valid(cache_path):
            text = self._read_cache(cache_path)
            if text:
                result = (
                    self._parse_json_response(text, series_id)
                    if use_json
                    else self._parse_csv_response(text, series_id)
                )
                if result is not None:
                    return result

        # --- Live fetch ---
        text = self._fetch_url(url)
        if text is not None:
            self._write_cache(cache_path, text)
            result = (
                self._parse_json_response(text, series_id)
                if use_json
                else self._parse_csv_response(text, series_id)
            )
            if result is not None:
                return result
            # Parsing failed even though fetch succeeded
            log_api_failure(
                self._logger,
                source="FRED",
                detail=f"Parse error for {series_id}",
                fallback="None",
            )
            return None

        # --- Fallback: stale cache ---
        stale = self._load_from_cache_if_any(url, series_id, use_json)
        if stale is not None:
            self._logger.warning(
                "STALE_CACHE | FRED series %s | using cached data beyond TTL",
                series_id,
            )
            return stale

        log_missing_data(
            self._logger,
            what=f"FRED series {series_id}",
            fallback=None,
        )
        return None

    def get_latest_levels(
        self, series_ids: list[str] | None = None
    ) -> dict[str, float]:
        """Return the latest non-NaN observation for each series.

        Rate series (DGS*, SOFR, EFFR, FEDFUNDS, T10YIE) are divided by 100
        so the returned values are decimal rates (e.g. 0.0525 for 5.25%).
        Non-rate series are returned as-is.

        Returns
        -------
        dict[str, float]
            Empty dict if all series fail; missing series omitted.
        """
        ids = series_ids if series_ids is not None else _DEFAULT_SERIES_IDS
        result: dict[str, float] = {}
        for sid in ids:
            series = self.get_series(sid)
            if series is None or series.empty:
                continue
            clean = series.dropna()
            if clean.empty:
                continue
            latest_val = float(clean.iloc[-1])
            if sid in _RATE_SERIES:
                latest_val = latest_val / 100.0
            result[sid] = latest_val
        return result

    def get_treasury_inputs(self) -> dict[str, Any]:
        """Return rate levels and histories suitable for treasury_module.

        Returns
        -------
        dict with keys:
            sofr       float  decimal (e.g. 0.053)
            dgs2       float  decimal
            dgs10      float  decimal
            dgs30      float  decimal
            sofr_history      list[float]  last ~252 observations /100
            dgs2_history      list[float]
            dgs10_history     list[float]
            dgs30_history     list[float]

        All keys are present; missing data is represented as NaN for scalars
        and [] for history lists.

        Notes
        -----
        History values are already divided by 100 (decimals) so they are ready
        for volatility/z-score calculations in the treasury module.
        """
        series_map = {
            "sofr": SOFR,
            "dgs2": DGS2,
            "dgs10": DGS10,
            "dgs30": DGS30,
        }
        out: dict[str, Any] = {}

        for key, sid in series_map.items():
            series = self.get_series(sid)
            if series is not None and not series.empty:
                clean = series.dropna()
                if not clean.empty:
                    out[key] = float(clean.iloc[-1]) / 100.0
                    # Last ~252 trading-day observations as decimal list
                    hist_raw = clean.iloc[-252:]
                    out[f"{key}_history"] = (hist_raw / 100.0).tolist()
                else:
                    out[key] = float("nan")
                    out[f"{key}_history"] = []
            else:
                out[key] = float("nan")
                out[f"{key}_history"] = []

        return out
