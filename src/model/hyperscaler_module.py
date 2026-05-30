"""Hyperscaler Module: stress analytics for public hyperscalers and DC-REITs.

This module models the financial sensitivity of large-cap hyperscalers
(MSFT, GOOGL, AMZN, META, ORCL) and datacenter REITs (DLR, EQIX) to
macroeconomic shocks defined in a Scenario.

Spec nuances honored:
  - Announced vs spent capex: financials represent *reported/spent* capex, not
    announced guidance; stressed_capex applies scenario.capex.capex_cut_pct
    multiplicatively (positive pct = cut, reducing capex).
  - Gross vs net floating: floating_share is gross share of total_debt exposed
    to SOFR; net floating would require interest-rate swap netting (not modeled).
  - ai_capex_share_override from entity_universe.yaml is used where available.
  - Placeholder financials are ORDER-OF-MAGNITUDE estimates (LOW CONFIDENCE)
    sourced from public annual reports / earnings calls ca. 2024-2025.
    They are NOT audited figures and must be refreshed via edgar before use.
  - Private-credit estimates not applicable here (hyperscalers are public).
  - MTM stress vs realized losses: stressed_interest_expense reflects
    mark-to-market floating rate pass-through, not realized refinancing losses.
  - CoreWeave is skipped if its CIK is None (pre-IPO / no EDGAR filing).
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from .. import config
from ..utils.logging import (
    get_logger,
    log_assumption_used,
    log_missing_data,
)
from ..utils.validation import bps_to_decimal, clamp, safe_div
from .scenarios import Scenario

logger = get_logger("ai_dc_model.hyperscaler")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class HyperscalerFinancials(BaseModel):
    """Financial snapshot for one hyperscaler or DC-REIT.

    All currency fields are in USD (no unit conversion is applied).
    Values are annual figures unless otherwise noted.

    LOW CONFIDENCE when source='assumption': these are order-of-magnitude
    placeholders derived from public disclosures ca. 2024-2025 and must be
    replaced with live EDGAR data before any quantitative use.
    """

    name: str
    ticker: str = ""
    cik: str | None = None

    # Fraction of capex attributable to AI workloads (0..1).
    # Announced plans may differ from realized spend; use with caution.
    ai_capex_share: float = 0.5

    # Data provenance: 'assumption' (placeholder) or 'edgar' (live data).
    source: str = "assumption"

    # Share of total_debt that is floating-rate (gross, pre-swap netting).
    floating_share: float = 0.30

    # P&L / cash flow fields (all optional; None means not available)
    capex: float | None = None
    operating_cash_flow: float | None = None
    free_cash_flow: float | None = None
    total_debt: float | None = None
    interest_expense: float | None = None
    operating_lease_liabilities: float | None = None
    finance_lease_liabilities: float | None = None
    short_term_debt: float | None = None
    long_term_debt: float | None = None
    cash_and_marketable_securities: float | None = None
    revenue: float | None = None
    operating_income: float | None = None
    segment_cloud_revenue: float | None = None
    # ebitda_proxy: operating_income + D&A when full D&A not modeled.
    ebitda_proxy: float | None = None

    # -----------------------------------------------------------------------
    # Derived ratios (all NaN-safe; return None when inputs are missing)
    # -----------------------------------------------------------------------

    def capex_to_revenue(self) -> float | None:
        """Capex / Revenue. None if either is missing."""
        if self.capex is None or self.revenue is None:
            return None
        return safe_div(self.capex, self.revenue)

    def capex_to_ocf(self) -> float | None:
        """Capex / Operating Cash Flow. None if either is missing."""
        if self.capex is None or self.operating_cash_flow is None:
            return None
        return safe_div(self.capex, self.operating_cash_flow)

    def fcf_after_capex(self) -> float | None:
        """Free cash flow after capex.

        Uses free_cash_flow directly if set (already net of capex per most
        definitions). Falls back to operating_cash_flow - capex.
        """
        if self.free_cash_flow is not None:
            return self.free_cash_flow
        if self.operating_cash_flow is not None and self.capex is not None:
            return self.operating_cash_flow - self.capex
        return None

    def net_debt(self) -> float | None:
        """Total debt minus cash and marketable securities."""
        if self.total_debt is None:
            return None
        cash = self.cash_and_marketable_securities or 0.0
        return self.total_debt - cash

    def interest_coverage(self) -> float | None:
        """(Operating income or EBITDA proxy) / Interest expense (ICR).

        Returns None if interest_expense is missing or zero.
        Prefers operating_income; falls back to ebitda_proxy.
        """
        numerator = self.operating_income if self.operating_income is not None else self.ebitda_proxy
        if numerator is None or self.interest_expense is None:
            return None
        return safe_div(numerator, self.interest_expense)

    def debt_to_ebitda_proxy(self) -> float | None:
        """Total debt / (EBITDA proxy or operating income).

        Prefers ebitda_proxy; falls back to operating_income.
        """
        denominator = self.ebitda_proxy if self.ebitda_proxy is not None else self.operating_income
        if self.total_debt is None or denominator is None:
            return None
        return safe_div(self.total_debt, denominator)

    def ai_capex_pressure_score(self) -> float:
        """Blend of AI capex share, capex/OCF, and leverage sign -> 0..1.

        Returns 0.5 when inputs are too thin to distinguish pressure levels
        (documented below). Higher values indicate more AI-capex-induced stress.

        Components (each normalized to 0..1, then blended):
          - ai_share_score   = ai_capex_share  (already 0..1)
          - capex_ocf_score  = clamp(capex/ocf, 0, 2) / 2   (2x OCF = full pressure)
          - leverage_score   = 0.7 if net_debt > 0 else 0.3

        Weights: 0.40 / 0.35 / 0.25 respectively.
        Falls back to 0.5 (neutral) if capex or OCF missing.
        """
        ai_share_score = clamp(self.ai_capex_share, 0.0, 1.0)

        cto = self.capex_to_ocf()
        if cto is None or math.isnan(cto):
            # Too thin to score; return neutral. (documented)
            return 0.5
        capex_ocf_score = clamp(cto, 0.0, 2.0) / 2.0

        nd = self.net_debt()
        if nd is None or math.isnan(nd):
            leverage_score = 0.5  # neutral when unknown
        else:
            leverage_score = 0.7 if nd > 0 else 0.3

        score = 0.40 * ai_share_score + 0.35 * capex_ocf_score + 0.25 * leverage_score
        return clamp(score, 0.0, 1.0)

    def refinancing_sensitivity(self) -> float:
        """Sensitivity to refinancing stress: 0 (low) .. 1 (high).

        Driven by short_term_debt / total_debt and net_debt / ebitda.
        Returns 0.5 (neutral) when data is too sparse to estimate.
        """
        if self.total_debt is None or self.total_debt == 0:
            return 0.5  # neutral; no debt means no refinancing risk but also no data

        # Component 1: short-term debt share (maturity wall proximity)
        st_share = 0.0
        if self.short_term_debt is not None:
            st_share = clamp(safe_div(self.short_term_debt, self.total_debt, default=0.0), 0.0, 1.0)

        # Component 2: net leverage relative to earnings capacity
        nd = self.net_debt()
        ebitda_or_oi = self.ebitda_proxy if self.ebitda_proxy is not None else self.operating_income
        if nd is not None and ebitda_or_oi is not None and ebitda_or_oi > 0:
            leverage_ratio = safe_div(nd, ebitda_or_oi, default=0.0)
            # Cap at 10x; normalize to 0..1
            leverage_score = clamp(leverage_ratio / 10.0, 0.0, 1.0)
        else:
            leverage_score = 0.5  # neutral

        score = 0.50 * st_share + 0.50 * leverage_score
        return clamp(score, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Module (stateful service object — plain class, not pydantic)
# ---------------------------------------------------------------------------

class HyperscalerModule:
    """Holds a list of HyperscalerFinancials and applies scenario shocks.

    Uses a plain class (not pydantic BaseModel) because it holds mutable
    state and service-level logic rather than pure data.
    """

    def __init__(self, entities: list[HyperscalerFinancials]) -> None:
        self._entities: list[HyperscalerFinancials] = entities
        # Index by name for fast lookup
        self._by_name: dict[str, HyperscalerFinancials] = {e.name: e for e in entities}

    @property
    def entities(self) -> list[HyperscalerFinancials]:
        return self._entities

    def apply_scenario(self, scenario: Scenario) -> dict[str, dict[str, Any]]:
        """Apply scenario shocks to each entity; return dict[name -> metrics].

        Stress mechanics:
          stressed_interest_expense = interest_expense + total_debt
              * floating_share * bps_to_decimal(sofr_bps)
            (MTM floating rate pass-through, NOT realized refinancing losses)

          stressed_capex = capex * (1 + capex_cut_pct)
            (positive capex_cut_pct reduces capex; negative increases it)

        Flags:
          fcf_after_capex_negative: stressed FCF is negative
          interest_coverage_below_2: stressed ICR < 2.0x
        """
        results: dict[str, dict[str, Any]] = {}
        sofr_bps = scenario.rates.sofr_bps
        capex_cut = scenario.capex.capex_cut_pct

        for entity in self._entities:
            # --- Stressed interest expense ---
            interest_delta: float | None = None
            stressed_interest: float | None = None

            if entity.interest_expense is not None and entity.total_debt is not None:
                interest_delta = (
                    entity.total_debt
                    * entity.floating_share
                    * bps_to_decimal(sofr_bps)
                )
                stressed_interest = entity.interest_expense + interest_delta
            elif entity.interest_expense is not None:
                stressed_interest = entity.interest_expense
                interest_delta = 0.0

            # --- Stressed capex ---
            stressed_capex: float | None = None
            if entity.capex is not None:
                stressed_capex = entity.capex * (1.0 + capex_cut)

            # --- Stressed FCF after capex ---
            # Use stressed values where available
            stressed_ocf = entity.operating_cash_flow  # OCF unchanged by these shocks
            stressed_fcf: float | None = None
            if entity.free_cash_flow is not None:
                # Approximate: reduce by interest delta (additional cash outflow)
                delta_int = interest_delta if interest_delta is not None else 0.0
                stressed_fcf = entity.free_cash_flow - delta_int
            elif stressed_ocf is not None and stressed_capex is not None:
                delta_int = interest_delta if interest_delta is not None else 0.0
                stressed_fcf = stressed_ocf - stressed_capex - delta_int

            # --- Stressed ICR (for flag) ---
            numerator = (
                entity.operating_income
                if entity.operating_income is not None
                else entity.ebitda_proxy
            )
            stressed_icr: float | None = None
            if numerator is not None and stressed_interest is not None and stressed_interest != 0:
                stressed_icr = safe_div(numerator, stressed_interest)

            # --- AI capex pressure score ---
            ai_pressure = entity.ai_capex_pressure_score()

            # --- Flags ---
            flags: dict[str, bool] = {
                "fcf_after_capex_negative": (
                    stressed_fcf is not None and stressed_fcf < 0
                ),
                "interest_coverage_below_2": (
                    stressed_icr is not None and stressed_icr < 2.0
                ),
            }

            results[entity.name] = {
                "stressed_interest_expense": stressed_interest,
                "interest_delta": interest_delta,
                "stressed_capex": stressed_capex,
                "fcf_after_capex": stressed_fcf,
                "ai_capex_pressure": ai_pressure,
                "flags": flags,
            }

        return results

    def summary(self, scenario: Scenario) -> dict[str, Any]:
        """High-level summary across all entities for a given scenario."""
        entity_results = self.apply_scenario(scenario)

        total_interest_delta = 0.0
        n_fcf_negative = 0
        n_icr_below_2 = 0
        names_flagged: list[str] = []

        for name, res in entity_results.items():
            if res["interest_delta"] is not None:
                total_interest_delta += res["interest_delta"]
            if res["flags"]["fcf_after_capex_negative"]:
                n_fcf_negative += 1
                names_flagged.append(name)
            if res["flags"]["interest_coverage_below_2"]:
                n_icr_below_2 += 1

        return {
            "n_entities": len(self._entities),
            "scenario_sofr_bps": scenario.rates.sofr_bps,
            "scenario_capex_cut_pct": scenario.capex.capex_cut_pct,
            "total_floating_interest_delta_usd": total_interest_delta,
            "n_fcf_after_capex_negative": n_fcf_negative,
            "n_interest_coverage_below_2": n_icr_below_2,
            "names_fcf_negative": names_flagged,
            "entity_details": entity_results,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Order-of-magnitude placeholder financials (LOW CONFIDENCE).
# Source: public annual reports / earnings releases ca. 2024-2025.
# NOT audited; refresh via EDGAR before quantitative use.
# Capex here is reported/spent capex, not announced guidance.
_ASSUMPTION_FINANCIALS: dict[str, dict[str, Any]] = {
    "Microsoft": dict(
        ticker="MSFT",
        capex=75e9,
        revenue=270e9,
        operating_cash_flow=130e9,
        total_debt=50e9,
        interest_expense=2e9,
        cash_and_marketable_securities=80e9,
        operating_income=110e9,
    ),
    "Alphabet": dict(
        ticker="GOOGL",
        capex=52e9,
        revenue=350e9,
        operating_cash_flow=135e9,
        total_debt=28e9,
        interest_expense=1e9,
        cash_and_marketable_securities=110e9,
        operating_income=110e9,
    ),
    "Amazon": dict(
        ticker="AMZN",
        capex=83e9,
        revenue=640e9,
        operating_cash_flow=115e9,
        total_debt=135e9,
        interest_expense=3e9,
        cash_and_marketable_securities=85e9,
        operating_income=68e9,
    ),
    "Meta Platforms": dict(
        ticker="META",
        capex=40e9,
        revenue=165e9,
        operating_cash_flow=90e9,
        total_debt=38e9,
        interest_expense=1e9,
        cash_and_marketable_securities=65e9,
        operating_income=70e9,
    ),
    "Oracle": dict(
        ticker="ORCL",
        capex=22e9,
        revenue=57e9,
        operating_cash_flow=20e9,
        total_debt=90e9,
        interest_expense=4e9,
        cash_and_marketable_securities=11e9,
        operating_income=18e9,
    ),
    "Digital Realty Trust": dict(
        ticker="DLR",
        capex=3e9,
        revenue=5.5e9,
        operating_cash_flow=2e9,
        total_debt=17e9,
        interest_expense=0.6e9,
        cash_and_marketable_securities=2e9,
        operating_income=0.8e9,
    ),
    "Equinix": dict(
        ticker="EQIX",
        capex=3e9,
        revenue=8.7e9,
        operating_cash_flow=3e9,
        total_debt=17e9,
        interest_expense=0.7e9,
        cash_and_marketable_securities=3e9,
        operating_income=1.5e9,
    ),
}

# Map common ticker aliases to canonical names in _ASSUMPTION_FINANCIALS
_TICKER_TO_NAME: dict[str, str] = {
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet",
    "GOOG": "Alphabet",
    "AMZN": "Amazon",
    "META": "Meta Platforms",
    "ORCL": "Oracle",
    "DLR": "Digital Realty Trust",
    "EQIX": "Equinix",
}


def build_hyperscaler_module(
    financials_by_name: dict[str, dict[str, Any]] | None = None,
) -> HyperscalerModule:
    """Construct a HyperscalerModule from entity universe + optional live data.

    Parameters
    ----------
    financials_by_name:
        Optional mapping of entity name -> financial field dict (source='edgar').
        When provided, live data takes precedence over assumption placeholders.

    Entity universe is loaded from entity_universe.yaml (hyperscalers +
    datacenter_operators). ai_capex_share_override.value is applied if present.

    CoreWeave is skipped if its cik field is None (pre-IPO / no EDGAR filing).

    Logs assumption use at INFO level and missing data at WARNING level.
    All placeholder financials are LOW CONFIDENCE.
    """
    universe = config.load_entity_universe()
    all_companies: list[dict[str, Any]] = list(
        universe.get("hyperscalers", []) + universe.get("datacenter_operators", [])
    )

    entities: list[HyperscalerFinancials] = []

    for company in all_companies:
        name: str = company.get("name", "")
        ticker: str = company.get("ticker", "")
        cik: str | None = company.get("cik")

        # CoreWeave: skip if no CIK (not yet a public filer)
        if ticker == "CRWV" and cik is None:
            logger.info(
                "SKIP | %s (ticker=%s) has no CIK; skipping (pre-IPO/no EDGAR)",
                name,
                ticker,
            )
            continue

        # Resolve ai_capex_share from override block or entity default
        ai_capex_share: float = 0.5
        override_block = company.get("ai_capex_share_override", {})
        if isinstance(override_block, dict) and "value" in override_block:
            ai_capex_share = float(override_block["value"])
            confidence = override_block.get("confidence", "unknown")
            log_assumption_used(
                logger,
                key=f"{name}.ai_capex_share_override",
                value=ai_capex_share,
                confidence=confidence,
            )
        else:
            fallback_share = config.assumption_value("default_ai_capex_share", 0.5)
            ai_capex_share = float(fallback_share)
            log_assumption_used(
                logger,
                key=f"{name}.ai_capex_share (default)",
                value=ai_capex_share,
                confidence="low",
            )

        # Resolve financial data: live > assumption placeholder
        fin_fields: dict[str, Any] = {}
        data_source = "assumption"

        # Check by canonical name first, then by ticker alias
        canonical = _TICKER_TO_NAME.get(ticker, name)

        if financials_by_name is not None:
            live = financials_by_name.get(name) or financials_by_name.get(canonical)
            if live:
                fin_fields = dict(live)
                data_source = "edgar"
                logger.info("LIVE_DATA | %s | using edgar financials", name)

        if not fin_fields:
            # Fall back to assumption placeholder
            placeholder = _ASSUMPTION_FINANCIALS.get(canonical) or _ASSUMPTION_FINANCIALS.get(name)
            if placeholder:
                fin_fields = dict(placeholder)
                data_source = "assumption"
                log_assumption_used(
                    logger,
                    key=f"{name}.financials (placeholder)",
                    value="order-of-magnitude ca. 2024-2025",
                    confidence="low",
                )
            else:
                log_missing_data(
                    logger,
                    what=f"{name} financials",
                    fallback="empty HyperscalerFinancials",
                )

        # Remove ticker from fin_fields if present (set separately)
        fin_fields.pop("ticker", None)

        entity = HyperscalerFinancials(
            name=name,
            ticker=ticker,
            cik=cik,
            ai_capex_share=ai_capex_share,
            source=data_source,
            **fin_fields,
        )
        entities.append(entity)

    logger.info(
        "BUILD | HyperscalerModule with %d entities", len(entities)
    )
    return HyperscalerModule(entities)
