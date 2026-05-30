"""EIA v2 API client with on-disk caching and graceful degradation.

Without an API key the client degrades gracefully: every method returns None
and the power module falls back to YAML base_assumptions. All errors are logged
but never raised to callers.

Key distinctions honoured:
- DC power demand vs total commercial electricity demand (sector='COM' captures
  commercial sector; datacenter subset is not available at API level and must be
  estimated separately).
- Retail price (sector-average $/MWh) vs wholesale/LMP (ISO-specific routes not
  stable in v2; wholesale_or_regional_prices() is best-effort / None in v0).
- natural_gas_price() targets Henry Hub spot via the EIA v2 natural-gas routes;
  degrades to None when unavailable.
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
    log_assumption_used,
    log_missing_data,
)

_LOG = get_logger("ai_dc_model.eia")

_EIA_BASE = "https://api.eia.gov/v2"


class EiaClient:
    """Thin wrapper around the EIA v2 REST API.

    Parameters
    ----------
    api_key:
        EIA API key.  Defaults to ``config.EIA_API_KEY``.  When None (or a
        placeholder) the client logs a warning and every method returns None.
    cache_dir:
        Directory for on-disk JSON caches.  Defaults to ``config.cache_dir()``.
    ttl_hours:
        Cache time-to-live in hours.  Defaults to ``config.cache_ttl_hours()``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        ttl_hours: float | None = None,
    ) -> None:
        self._api_key: str | None = api_key if api_key is not None else config.EIA_API_KEY
        self._cache_dir: Path = cache_dir if cache_dir is not None else config.cache_dir()
        self._ttl_seconds: float = (
            ttl_hours if ttl_hours is not None else config.cache_ttl_hours()
        ) * 3600.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_path(self, url: str, params: dict[str, Any]) -> Path:
        """Return a deterministic cache file path for the given request."""
        # Sort params (excluding api_key) for a stable key so cache hits are
        # independent of insertion order.
        safe_params = {k: v for k, v in params.items() if k != "api_key"}
        key_material = url + json.dumps(safe_params, sort_keys=True)
        digest = hashlib.sha256(key_material.encode()).hexdigest()
        return self._cache_dir / f"eia_{digest}.json"

    def _load_cache(self, path: Path) -> dict | None:
        """Return cached JSON if it exists and is within TTL, else None."""
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self._ttl_seconds:
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save_cache(self, path: Path, data: dict) -> None:
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except Exception:
            pass  # cache write failures are non-fatal

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict | None:
        """Fetch JSON from url, injecting api_key and honouring cache/offline.

        Returns the parsed JSON dict, or None on any error.  Never raises.
        """
        if not self._api_key:
            log_missing_data(
                _LOG,
                what="EIA_API_KEY",
                fallback="None — power module will use YAML assumptions",
            )
            return None

        params = dict(params or {})
        params["api_key"] = self._api_key

        cache_path = self._cache_path(url, params)

        # --- offline mode: cache only ---
        if config.is_offline():
            cached = self._load_cache(cache_path)
            if cached is None:
                log_missing_data(
                    _LOG,
                    what=f"EIA offline cache for {url}",
                    fallback=None,
                )
            return cached

        # --- try cache first (within TTL) ---
        cached = self._load_cache(cache_path)
        if cached is not None:
            return cached

        # --- live request ---
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self._save_cache(cache_path, data)
            return data
        except requests.exceptions.Timeout:
            log_api_failure(_LOG, source="EIA", detail=f"timeout fetching {url}")
        except requests.exceptions.HTTPError as exc:
            log_api_failure(
                _LOG,
                source="EIA",
                detail=f"HTTP {exc.response.status_code} for {url}",
            )
        except Exception as exc:
            log_api_failure(_LOG, source="EIA", detail=f"{type(exc).__name__}: {exc}")

        # --- fall back to stale cache on network failure ---
        stale = self._load_cache.__func__(self, cache_path) if False else None  # type: ignore[attr-defined]
        # Re-implement stale fallback without TTL check:
        if cache_path.exists():
            try:
                with cache_path.open("r", encoding="utf-8") as fh:
                    stale = json.load(fh)
                _LOG.warning("EIA | using stale cache for %s", url)
                return stale
            except Exception:
                pass

        return None

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def retail_electricity_price(
        self, state: str | None = None, sector: str = "COM"
    ) -> float | None:
        """Return the latest retail electricity price in $/MWh.

        Fetches from the EIA v2 ``electricity/retail-sales/data/`` endpoint.
        The EIA reports in cents/kWh; this method converts to $/MWh (multiply
        by 10: 1 cent/kWh = $0.01/kWh = $10/MWh).

        Parameters
        ----------
        state:
            Two-letter state code (e.g. ``'TX'``).  None = national average.
        sector:
            EIA sector ID; ``'COM'`` = commercial (default), ``'IND'`` =
            industrial, ``'RES'`` = residential, ``'ALL'`` = all sectors.

        Returns
        -------
        float | None
            Price in $/MWh, or None when data is unavailable.

        Notes
        -----
        The ``COM`` sector average includes *all* commercial users, not only
        datacenters.  Datacenter-specific electricity prices are not available
        at the API level and must be estimated using utility-rate assumptions.
        """
        url = f"{_EIA_BASE}/electricity/retail-sales/data/"
        params: dict[str, Any] = {
            "data[]": "price",
            "facets[sectorid][]": sector,
            "frequency": "monthly",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 1,
            "offset": 0,
        }
        if state:
            params["facets[stateid][]"] = state.upper()

        data = self._get_json(url, params)
        if data is None:
            return None

        try:
            rows = data["response"]["data"]
            if not rows:
                log_missing_data(
                    _LOG,
                    what=f"EIA retail price rows (sector={sector}, state={state})",
                    fallback=None,
                )
                return None
            price_cents_per_kwh = float(rows[0]["price"])
            # 1 cent/kWh * 10 = $/MWh
            price_usd_per_mwh = price_cents_per_kwh * 10.0
            log_assumption_used(
                _LOG,
                key=f"eia.retail_electricity_price(sector={sector}, state={state})",
                value=price_usd_per_mwh,
                confidence="medium",
            )
            return price_usd_per_mwh
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log_api_failure(
                _LOG,
                source="EIA",
                detail=f"retail price parse error: {exc}",
            )
            return None

    def wholesale_or_regional_prices(self) -> dict | None:
        """Return wholesale / regional electricity prices (best-effort).

        In EIA API v2 the ISO-level LMP series require routes that are not yet
        stable.  This method is a stub that returns None in v0; the power
        module should fall back to YAML assumptions for wholesale prices.

        Returns
        -------
        dict | None
            Mapping of region -> price, or None when unavailable.
        """
        # ISO LMP data requires specific routes per ISO (MISO, PJM, CAISO …)
        # which are not stably available via EIA v2 in v0.  Log and degrade.
        _LOG.info(
            "EIA | wholesale_or_regional_prices: ISO LMP routes not yet "
            "implemented in v0; returning None"
        )
        return None

    def natural_gas_price(self) -> float | None:
        """Return the latest Henry Hub natural gas spot price in $/MMBtu.

        Targets the EIA v2 ``natural-gas/pri/fut/`` or ``natural-gas/pri/sum/``
        series for Henry Hub.  Degrades gracefully to None when data is
        unavailable.

        Returns
        -------
        float | None
            Price in $/MMBtu (Henry Hub), or None when unavailable.
        """
        # Henry Hub weekly spot: EIA series ENG.RNGWHHD.W
        # Available via the v2 seriesid route.
        url = f"{_EIA_BASE}/natural-gas/pri/sum/data/"
        params: dict[str, Any] = {
            "data[]": "value",
            "facets[process][]": "PUS",  # Henry Hub spot price
            "frequency": "weekly",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 1,
            "offset": 0,
        }

        data = self._get_json(url, params)
        if data is None:
            return None

        try:
            rows = data["response"]["data"]
            if not rows:
                log_missing_data(
                    _LOG,
                    what="EIA Henry Hub natural gas price",
                    fallback=None,
                )
                return None
            price = float(rows[0]["value"])
            log_assumption_used(
                _LOG,
                key="eia.natural_gas_price_henry_hub_usd_per_mmbtu",
                value=price,
                confidence="medium",
            )
            return price
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log_api_failure(
                _LOG,
                source="EIA",
                detail=f"natural gas price parse error: {exc}",
            )
            return None

    def get_power_inputs(self) -> dict[str, float | None]:
        """Return a dict of power-related inputs for the model.

        Keys
        ----
        retail_price_com_usd_per_mwh : float | None
            National commercial-sector retail electricity price in $/MWh.
            This is the sector average; datacenter-specific rates must be
            estimated separately (DCs typically negotiate custom tariffs).
        natural_gas_usd_per_mmbtu : float | None
            Henry Hub natural gas spot price in $/MMBtu (relevant for gas-
            based generation and marginal-cost estimation).

        Returns an empty dict (not None) so callers can always do `.get()`.
        Logs each missing field individually.
        """
        result: dict[str, float | None] = {}

        try:
            retail = self.retail_electricity_price(state=None, sector="COM")
            result["retail_price_com_usd_per_mwh"] = retail
            if retail is None:
                log_missing_data(
                    _LOG,
                    what="EIA commercial retail electricity price",
                    fallback="YAML base_assumptions",
                )
        except Exception as exc:
            log_api_failure(
                _LOG,
                source="EIA",
                detail=f"get_power_inputs retail_price: {exc}",
            )
            result["retail_price_com_usd_per_mwh"] = None

        try:
            ng = self.natural_gas_price()
            result["natural_gas_usd_per_mmbtu"] = ng
            if ng is None:
                log_missing_data(
                    _LOG,
                    what="EIA Henry Hub natural gas price",
                    fallback="YAML base_assumptions",
                )
        except Exception as exc:
            log_api_failure(
                _LOG,
                source="EIA",
                detail=f"get_power_inputs natural_gas: {exc}",
            )
            result["natural_gas_usd_per_mmbtu"] = None

        return result
