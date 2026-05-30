"""Treasury module: risk-free rate blending, discount/refinancing/cap-rate shocks,
securitization openness scoring, and the Treasury Stress Index (TSI).

Spec nuances honoured:
- TSI uses EQUAL-weight z-scores from base_assumptions.yaml 'treasury_stress_index';
  MISSING components are DROPPED (not zero-filled).
- rate_vol and auction_tail contribute ONLY when their history series is present.
- For components without history: pseudo-z = (scenario shock bps) / 100 when a
  scenario is provided; otherwise the component is dropped entirely.
- securitization_openness_score clamps to [0, 1] and accounts for haircut delta
  and adverse refinancing_probability_delta.
- Private-credit outputs in sibling modules are LOW-CONFIDENCE; mark estimates
  accordingly in callers.
- Distinguishes MTM stress vs realised losses, announced vs spent capex,
  DC power demand vs total commercial demand (not directly surfaced here but noted
  for downstream consumers of the base_rates and stress index).
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from .. import config
from ..utils.logging import (
    get_logger,
    log_assumption_used,
    log_missing_data,
)
from ..utils.validation import bps_to_decimal, clamp, latest_zscore
from .scenarios import Scenario

_logger = get_logger("ai_dc_model.treasury")

# ---------------------------------------------------------------------------
# Pydantic data holder
# ---------------------------------------------------------------------------

class TreasuryInputs(BaseModel):
    """Current rate levels plus optional history series for z-score computation.

    All rates are expressed as decimals (e.g. 0.045 for 4.5%).
    History lists contain observations in chronological order (oldest first).
    """

    # current levels
    sofr: float | None = None
    dgs2: float | None = None
    dgs10: float | None = None
    dgs30: float | None = None
    term_premium: float | None = None
    rate_vol: float | None = None
    auction_tail: float | None = None
    inflation_expectations: float | None = None

    # optional history for z-score calculation
    sofr_history: list[float] | None = Field(default=None)
    dgs2_history: list[float] | None = Field(default=None)
    dgs10_history: list[float] | None = Field(default=None)
    dgs30_history: list[float] | None = Field(default=None)
    term_premium_history: list[float] | None = Field(default=None)
    rate_vol_history: list[float] | None = Field(default=None)
    auction_tail_history: list[float] | None = Field(default=None)
    inflation_expectations_history: list[float] | None = Field(default=None)


# ---------------------------------------------------------------------------
# Stateful service object (plain class — pydantic forbids arbitrary attrs)
# ---------------------------------------------------------------------------

class TreasuryModule:
    """Applies treasury-rate shocks and computes the Treasury Stress Index.

    Parameters
    ----------
    inputs:
        Current rate levels and optional history series.
    base_rates:
        Dict mapping 'sofr', '2y', '10y', '30y' to decimal rates.
        Defaults are derived from ``inputs`` when not supplied.
    """

    def __init__(
        self,
        inputs: TreasuryInputs,
        base_rates: dict | None = None,
    ) -> None:
        self._inputs = inputs

        # Resolve base_rates: prefer explicit argument, fall back to inputs.
        if base_rates is not None:
            self.base_rates: dict[str, float] = base_rates
        else:
            self.base_rates = {
                "sofr": inputs.sofr if inputs.sofr is not None else math.nan,
                "2y": inputs.dgs2 if inputs.dgs2 is not None else math.nan,
                "10y": inputs.dgs10 if inputs.dgs10 is not None else math.nan,
                "30y": inputs.dgs30 if inputs.dgs30 is not None else math.nan,
            }

    # ------------------------------------------------------------------
    # Rate shock helpers
    # ------------------------------------------------------------------

    def base_risk_free_funding_rate(self, scenario: Scenario | None = None) -> float:
        """Blend 0.5*SOFR + 0.5*DGS10, with scenario basis-point shocks applied.

        Returns a decimal rate.
        """
        sofr_base = self.base_rates.get("sofr", math.nan)
        dgs10_base = self.base_rates.get("10y", math.nan)

        sofr_shock: float = 0.0
        dgs10_shock: float = 0.0
        if scenario is not None:
            sofr_shock = bps_to_decimal(scenario.rates.sofr_bps)
            dgs10_shock = bps_to_decimal(scenario.rates.dgs10_bps)

        sofr_rate = sofr_base + sofr_shock if not math.isnan(sofr_base) else math.nan
        dgs10_rate = dgs10_base + dgs10_shock if not math.isnan(dgs10_base) else math.nan

        # If either leg is missing, use the other; if both missing return NaN.
        if math.isnan(sofr_rate) and math.isnan(dgs10_rate):
            return math.nan
        if math.isnan(sofr_rate):
            return dgs10_rate
        if math.isnan(dgs10_rate):
            return sofr_rate
        return 0.5 * sofr_rate + 0.5 * dgs10_rate

    def discount_rate_shock(self, scenario: Scenario) -> float:
        """Additive shock to the discount rate: DGS10 bps + term_premium bps -> decimal."""
        return bps_to_decimal(
            scenario.rates.dgs10_bps + scenario.rates.term_premium_bps
        )

    def refinancing_cost_shock(self, scenario: Scenario) -> float:
        """Additive shock to refinancing cost: DGS10 bps + credit_spread bps -> decimal."""
        return bps_to_decimal(
            scenario.rates.dgs10_bps + scenario.credit.credit_spread_bps
        )

    def cap_rate_shock(self, scenario: Scenario) -> float:
        """Additive shock to cap rate from scenario -> decimal."""
        return bps_to_decimal(scenario.credit.cap_rate_bps)

    def securitization_openness_score(self, scenario: Scenario) -> float:
        """Score in [0, 1] for how open securitization markets are under this scenario.

        Starts at 0.9 (normally open). Drops to 0.2 if securitization_open is False.
        Subtracts securitization_haircut_delta and the magnitude of any adverse
        refinancing_probability_delta.
        """
        score: float = 0.9
        if not scenario.credit.securitization_open:
            score = 0.2

        score -= scenario.credit.securitization_haircut_delta
        # Only penalise adverse (negative) refinancing probability deltas.
        score -= abs(min(0.0, scenario.credit.refinancing_probability_delta))
        return clamp(score, 0.0, 1.0)

    # ------------------------------------------------------------------
    # Treasury Stress Index
    # ------------------------------------------------------------------

    def treasury_stress_index(self, scenario: Scenario | None = None) -> float:
        """Weighted sum of z-scores across available treasury stress components.

        Components: dgs10, dgs30, sofr, term_premium, rate_vol, auction_tail.
        Weights from config key 'treasury_stress_index' -> component_weights.

        Z-score is:
          - latest_zscore(history) if history is present and has >= 2 points.
          - pseudo-z = (shock bps) / 100 if scenario given and history absent.
          - Component DROPPED if no history and no scenario.

        rate_vol and auction_tail contribute ONLY when their history is present.
        Missing components are DROPPED (not zero-filled).
        """
        # Map component name -> (scenario shock bps, history list)
        # rate_vol / auction_tail are history-only (no scenario shock field).
        component_map: dict[str, tuple[float | None, list[float] | None]] = {
            "dgs10": (
                scenario.rates.dgs10_bps if scenario is not None else None,
                self._inputs.dgs10_history,
            ),
            "dgs30": (
                scenario.rates.dgs30_bps if scenario is not None else None,
                self._inputs.dgs30_history,
            ),
            "sofr": (
                scenario.rates.sofr_bps if scenario is not None else None,
                self._inputs.sofr_history,
            ),
            "term_premium": (
                scenario.rates.term_premium_bps if scenario is not None else None,
                self._inputs.term_premium_history,
            ),
            # rate_vol and auction_tail: history-only, no scenario shock mapping.
            "rate_vol": (None, self._inputs.rate_vol_history),
            "auction_tail": (None, self._inputs.auction_tail_history),
        }

        tsi_config = config.assumption_value("treasury_stress_index")
        if tsi_config is None:
            log_missing_data(
                _logger,
                what="treasury_stress_index config",
                fallback="equal weights 1.0",
            )
            weights: dict[str, float] = {k: 1.0 for k in component_map}
        else:
            weights = tsi_config.get("component_weights", {k: 1.0 for k in component_map})

        total: float = 0.0
        for name, (shock_bps, history) in component_map.items():
            weight = weights.get(name, 0.0)
            if weight == 0.0:
                continue

            if history is not None and len(history) >= 2:
                z = latest_zscore(history)
            elif name in ("rate_vol", "auction_tail"):
                # These two ONLY count when history is present.
                continue
            elif shock_bps is not None:
                # Pseudo-z: shock in bps / 100.
                z = shock_bps / 100.0
            else:
                # No history and no scenario -> drop.
                continue

            total += weight * z

        return total

    def treasury_stress_components(self, scenario: Scenario) -> dict[str, float]:
        """Return per-component contribution (weight * z) to the TSI.

        Dropped components are absent from the returned dict.
        """
        component_map: dict[str, tuple[float | None, list[float] | None]] = {
            "dgs10": (scenario.rates.dgs10_bps, self._inputs.dgs10_history),
            "dgs30": (scenario.rates.dgs30_bps, self._inputs.dgs30_history),
            "sofr": (scenario.rates.sofr_bps, self._inputs.sofr_history),
            "term_premium": (scenario.rates.term_premium_bps, self._inputs.term_premium_history),
            "rate_vol": (None, self._inputs.rate_vol_history),
            "auction_tail": (None, self._inputs.auction_tail_history),
        }

        tsi_config = config.assumption_value("treasury_stress_index")
        weights: dict[str, float]
        if tsi_config is None:
            weights = {k: 1.0 for k in component_map}
        else:
            weights = tsi_config.get("component_weights", {k: 1.0 for k in component_map})

        contributions: dict[str, float] = {}
        for name, (shock_bps, history) in component_map.items():
            weight = weights.get(name, 0.0)
            if weight == 0.0:
                continue
            if history is not None and len(history) >= 2:
                z = latest_zscore(history)
            elif name in ("rate_vol", "auction_tail"):
                continue
            elif shock_bps is not None:
                z = shock_bps / 100.0
            else:
                continue
            contributions[name] = weight * z

        return contributions

    def summary(self, scenario: Scenario | None = None) -> dict:
        """Return a summary dict of all computed quantities plus base_rates."""
        out: dict = {
            "base_rates": dict(self.base_rates),
            "base_risk_free_funding_rate": self.base_risk_free_funding_rate(scenario),
            "treasury_stress_index": self.treasury_stress_index(scenario),
        }
        if scenario is not None:
            out["discount_rate_shock"] = self.discount_rate_shock(scenario)
            out["refinancing_cost_shock"] = self.refinancing_cost_shock(scenario)
            out["cap_rate_shock"] = self.cap_rate_shock(scenario)
            out["securitization_openness_score"] = self.securitization_openness_score(scenario)
            out["treasury_stress_components"] = self.treasury_stress_components(scenario)
        return out


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

_DEFAULT_SOFR = 0.0433
_DEFAULT_DGS2 = 0.0430
_DEFAULT_DGS10 = 0.0450
_DEFAULT_DGS30 = 0.0470
_DEFAULT_TERM_PREMIUM = 0.0


def build_treasury_module(
    base_rates: dict | None = None,
    fred_data: dict | None = None,
) -> TreasuryModule:
    """Construct a TreasuryModule from live FRED data or assumption defaults.

    Parameters
    ----------
    base_rates:
        Optional override for {'sofr','2y','10y','30y'} decimal rates. When
        omitted the module derives them from inputs.
    fred_data:
        Dict with keys sofr, dgs2, dgs10, dgs30 (current decimal levels) and
        optional *_history keys (list[float]). When absent, hard-coded defaults
        are used and logged as assumptions.

    Verification
    ------------
    The caller should check that:
      - base_case  TSI ~ 0   (no history -> all zero pseudo-z for zero-shock scenario)
      - combined_severe_case TSI > high_for_longer TSI
    """
    if fred_data is not None:
        inputs = TreasuryInputs(
            sofr=fred_data.get("sofr"),
            dgs2=fred_data.get("dgs2"),
            dgs10=fred_data.get("dgs10"),
            dgs30=fred_data.get("dgs30"),
            term_premium=fred_data.get("term_premium"),
            rate_vol=fred_data.get("rate_vol"),
            auction_tail=fred_data.get("auction_tail"),
            inflation_expectations=fred_data.get("inflation_expectations"),
            sofr_history=fred_data.get("sofr_history"),
            dgs2_history=fred_data.get("dgs2_history"),
            dgs10_history=fred_data.get("dgs10_history"),
            dgs30_history=fred_data.get("dgs30_history"),
            term_premium_history=fred_data.get("term_premium_history"),
            rate_vol_history=fred_data.get("rate_vol_history"),
            auction_tail_history=fred_data.get("auction_tail_history"),
            inflation_expectations_history=fred_data.get("inflation_expectations_history"),
        )
    else:
        # Use hard-coded defaults; log each as an assumption.
        log_assumption_used(
            _logger,
            key="sofr",
            value=_DEFAULT_SOFR,
            confidence="medium",
        )
        log_assumption_used(
            _logger,
            key="dgs2",
            value=_DEFAULT_DGS2,
            confidence="medium",
        )
        log_assumption_used(
            _logger,
            key="dgs10",
            value=_DEFAULT_DGS10,
            confidence="medium",
        )
        log_assumption_used(
            _logger,
            key="dgs30",
            value=_DEFAULT_DGS30,
            confidence="medium",
        )
        log_assumption_used(
            _logger,
            key="term_premium",
            value=_DEFAULT_TERM_PREMIUM,
            confidence="low",
        )
        inputs = TreasuryInputs(
            sofr=_DEFAULT_SOFR,
            dgs2=_DEFAULT_DGS2,
            dgs10=_DEFAULT_DGS10,
            dgs30=_DEFAULT_DGS30,
            term_premium=_DEFAULT_TERM_PREMIUM,
        )

    return TreasuryModule(inputs=inputs, base_rates=base_rates)
