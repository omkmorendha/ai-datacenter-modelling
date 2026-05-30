"""Private-credit sector-level module for AI datacenter macro model.

Models the aggregate private-credit book that is exposed to AI-datacenter
financing.  All figures are SECTOR-LEVEL ESTIMATES and are LOW-CONFIDENCE:
private-credit markets are opaque, marks are delayed, and the true outstanding
is inferred from public market commentary rather than observed data.

Key spec nuances honored:
  - gross vs net floating exposure: gross = outstanding * floating_rate_share;
    net = gross * (1 - hedge_ratio).  Only net transmits SOFR shocks.
  - MTM stress (nav_haircut applied to outstanding) is DISTINCT from realized
    expected loss (PD * LGD * outstanding).  Both are computed; callers should
    not sum them.
  - Committed vs drawn: this module models the DRAWN book; new_lending_capacity
    impact covers committed-but-undrawn / pipeline reduction.
  - All interest figures are ANNUALIZED.
  - private_credit investor_base shares must sum to ~1.0; if not, a warning is
    logged and allocation is rescaled proportionally rather than raising.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import config
from ..utils.logging import (
    get_logger,
    log_assumption_used,
    log_missing_data,
)
from ..utils.validation import (
    bps_to_decimal,
    clamp,
    require_shares_sum_to_one,
)
from .scenarios import Scenario

_logger = get_logger("ai_dc_model.private_credit")

# ---------------------------------------------------------------------------
# Default investor-base fallback (in case YAML is absent)
# ---------------------------------------------------------------------------
_DEFAULT_INVESTOR_BASE: dict[str, float] = {
    "bdcs": 0.18,
    "private_credit_funds": 0.30,
    "insurers": 0.22,
    "pensions": 0.15,
    "banks": 0.08,
    "sovereign_wealth": 0.05,
    "other": 0.02,
}


class PrivateCreditBook(BaseModel):
    """Sector-level private-credit book for AI-datacenter exposure.

    ALL fields are LOW-CONFIDENCE estimates derived from public market
    commentary, BIS/IMF/FSB framing, and analyst surveys.  Do NOT treat as
    observed data.

    Attributes
    ----------
    outstanding:
        Total drawn private-credit exposure to AI datacenters (USD).
    floating_rate_share:
        Fraction of the book priced off a floating reference (SOFR + spread).
        Gross floating = outstanding * floating_rate_share.
    hedge_ratio:
        Fraction of gross floating exposure hedged/swapped to fixed at the
        borrower or fund level.  Net floating = gross * (1 - hedge_ratio).
    average_spread_bps:
        Portfolio-average credit spread over SOFR (basis points).
    average_ltv:
        Loan-to-value at origination (ratio, 0-1).
    average_dscr:
        Portfolio-average debt-service-coverage ratio at origination.
    default_probability:
        Base annual probability of default (PD) for the drawn book.
    recovery_rate:
        Expected recovery rate on defaulted loans (ratio, 0-1).
    investor_base:
        Share of drawn book held by each investor type; should sum to ~1.0.
    """

    outstanding: float = 250_000_000_000.0
    floating_rate_share: float = 0.85
    hedge_ratio: float = 0.20
    average_spread_bps: float = 550.0
    average_ltv: float = 0.60
    average_dscr: float = 1.45
    default_probability: float = 0.03
    recovery_rate: float = 0.55
    investor_base: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_INVESTOR_BASE)
    )

    # ------------------------------------------------------------------
    # 1. Exposure decomposition
    # ------------------------------------------------------------------

    def net_floating_exposure(self) -> float:
        """Net floating-rate exposure in USD after hedging.

        Gross floating = outstanding * floating_rate_share.
        Net floating   = gross * (1 - hedge_ratio).

        Only *net* floating transmits SOFR rate shocks into debt service;
        the hedge reduces (but does not eliminate) rate sensitivity.
        The gross figure matters for counterparty / derivative risk, which is
        NOT modelled here.
        """
        gross = self.outstanding * self.floating_rate_share
        return gross * (1.0 - self.hedge_ratio)

    # ------------------------------------------------------------------
    # 2. Interest burden
    # ------------------------------------------------------------------

    def base_interest(self, sofr_base: float = 0.0433) -> float:
        """Annual base-case interest income / cost for the full drawn book (USD).

        Applies the base SOFR rate plus the average credit spread to the full
        outstanding, regardless of fixed/floating mix.  This is an
        ANNUALIZED, GROSS figure before hedging costs.

        Parameters
        ----------
        sofr_base:
            Base SOFR rate (decimal, e.g. 0.0433 = 4.33 %).
        """
        all_in_rate = sofr_base + bps_to_decimal(self.average_spread_bps)
        return self.outstanding * all_in_rate

    def interest_burden(self, scenario: Scenario, sofr_base: float = 0.0433) -> float:
        """Incremental annual interest cost from the SOFR shock in a scenario (USD).

        Applies the scenario's SOFR shock ONLY to the NET floating exposure,
        because the hedge converts the rest to effectively fixed.  This is the
        *additional* annualized cash burden vs the base case, not the total
        interest bill.

        Parameters
        ----------
        scenario:
            Scenario carrying the SOFR shock (rates.sofr_bps).
        sofr_base:
            Base SOFR rate (decimal); informational only here — the incremental
            burden is shock * net_floating.
        """
        shock_decimal = bps_to_decimal(scenario.rates.sofr_bps)
        return self.net_floating_exposure() * shock_decimal

    # ------------------------------------------------------------------
    # 3. Credit stress
    # ------------------------------------------------------------------

    def stressed_default_probability(self, scenario: Scenario) -> float:
        """Stressed annual PD after applying scenario default_probability_mult.

        Clamped to [0, 1].  The multiplier is applied to the base PD, not
        the PD increment; a mult of 2.0 doubles the base annual PD.
        """
        stressed = self.default_probability * scenario.private_credit.default_probability_mult
        return clamp(stressed, 0.0, 1.0)

    def stressed_recovery(self, scenario: Scenario) -> float:
        """Stressed recovery rate after applying scenario recovery_rate_delta.

        recovery_rate_delta is additive in native units (e.g. -0.10 lowers
        recovery by 10 pp).  Clamped to [0, 1].
        """
        stressed = self.recovery_rate + scenario.private_credit.recovery_rate_delta
        return clamp(stressed, 0.0, 1.0)

    def expected_loss(self, scenario: Scenario) -> float:
        """Expected REALIZED credit loss for the drawn book (USD).

        EL = outstanding * stressed_PD * (1 - stressed_recovery).

        This is a REALIZED concept — the economic loss if defaults crystallize.
        It is NOT the same as mark-to-market loss.  Private-credit marks are
        typically lagged / smoothed; realized losses emerge over 12-24 months.
        Do NOT add this to mark_to_market_loss — they measure different things.
        """
        stressed_pd = self.stressed_default_probability(scenario)
        lgd = 1.0 - self.stressed_recovery(scenario)
        return self.outstanding * stressed_pd * lgd

    def mark_to_market_loss(self, scenario: Scenario) -> float:
        """Mark-to-market (NAV haircut) loss for the drawn book (USD).

        MTM loss = outstanding * nav_haircut.

        This is an INSTANTANEOUS, PAPER loss concept — not necessarily realized.
        Private-credit funds use quarterly or semi-annual NAV marks; the true
        economic loss may differ from the reported mark.  Distinct from
        expected_loss (PD * LGD), which is a forward-realized concept.
        Do NOT add this to expected_loss.

        A loss is non-negative: a negative nav_haircut (a NAV *gain*) is floored
        to zero here so it cannot flow as a negative "loss" into investor-loss
        allocation or the contagion shock vector.
        """
        return self.outstanding * max(0.0, scenario.private_credit.nav_haircut)

    # ------------------------------------------------------------------
    # 4. Liquidity / systemic stress scores
    # ------------------------------------------------------------------

    def liquidity_stress_score(self, scenario: Scenario) -> float:
        """Composite liquidity stress score for the private-credit book (0-1).

        Blends four stress signals with equal weight (0.25 each):
          1. NAV haircut                     (weight 0.25) — valuation stress
          2. (default_mult - 1) / 2          (weight 0.25) — credit deterioration
             normalised so a doubling of PD (mult=2) contributes 0.5 to its component
          3. |new_lending_capacity_pct|      (weight 0.25) — pipeline/market closure
          4. credit_spread_bps / 500         (weight 0.25) — spread widening signal

        All components are individually clamped to [0, 1] before blending.
        The composite score is then clamped to [0, 1].
        """
        w = 0.25

        nav_component = clamp(scenario.private_credit.nav_haircut, 0.0, 1.0)

        default_mult = scenario.private_credit.default_probability_mult
        default_component = clamp((default_mult - 1.0) / 2.0, 0.0, 1.0)

        lending_component = clamp(
            abs(scenario.private_credit.new_lending_capacity_pct), 0.0, 1.0
        )

        spread_component = clamp(
            bps_to_decimal(scenario.credit.credit_spread_bps) / 0.05, 0.0, 1.0
        )
        # Note: 500 bps = 0.05 decimal; dividing by 0.05 normalises to [0,1]
        # for spreads up to 500 bps.

        score = w * (nav_component + default_component + lending_component + spread_component)
        return clamp(score, 0.0, 1.0)

    def investor_loss_allocation(self, scenario: Scenario) -> dict[str, float]:
        """Allocate the gross loss (max of EL and MTM) across the investor base.

        Uses max(expected_loss, mark_to_market_loss) as the loss to allocate.
        This captures the more binding constraint: realized credit losses or
        mark-to-market NAV losses (whichever is larger at a point in time).

        If investor_base shares do not sum to 1.0 within tolerance, a warning
        is logged and shares are rescaled proportionally (not raised).

        Returns
        -------
        dict investor_type -> USD loss
        """
        total_loss = max(self.expected_loss(scenario), self.mark_to_market_loss(scenario))

        shares = dict(self.investor_base)
        total_share = sum(shares.values())

        # Tolerant normalization — log warning instead of raising
        try:
            require_shares_sum_to_one(shares, tol=1e-3, label="private_credit.investor_base")
        except ValueError as exc:
            _logger.warning(
                "INVESTOR_BASE_SHARES | %s | rescaling proportionally", exc
            )
            if total_share > 0:
                shares = {k: v / total_share for k, v in shares.items()}

        return {investor: share * total_loss for investor, share in shares.items()}

    def redemption_gating_stress_proxy(self, scenario: Scenario) -> float:
        """Proxy score for open-end fund redemption / gating risk (0-1).

        Open-end BDCs and private-credit funds face redemption queues when NAV
        falls and liquidity dries up.  This proxy multiplies the liquidity
        stress score by the combined share of open-end-style investors (BDCs +
        private_credit_funds), who are most exposed to redemption gating.

        Closed-end vehicles (insurers, pensions, banks, sovereign wealth) are
        structurally insulated from redemption runs; they are deliberately
        excluded from the numerator.

        Returns a score in [0, 1] — higher means greater systemic risk from
        fund-level gating and fire-sale dynamics.
        """
        open_end_share = self.investor_base.get("bdcs", 0.0) + self.investor_base.get(
            "private_credit_funds", 0.0
        )
        return clamp(self.liquidity_stress_score(scenario) * open_end_share, 0.0, 1.0)

    # ------------------------------------------------------------------
    # 5. Forward lending capacity
    # ------------------------------------------------------------------

    def future_lending_capacity_impact(self, scenario: Scenario) -> dict[str, float]:
        """Dollar and percentage impact on future lending capacity.

        new_lending_capacity_pct is a fractional change in the pipeline
        (e.g. -0.30 = 30 % contraction in committed/pipeline origination).
        Negative values represent credit-crunch / market-closure scenarios.

        Returns
        -------
        dict with:
          usd_delta  — change in USD lending capacity (can be negative)
          pct        — the raw scenario parameter (fractional)
        """
        pct = scenario.private_credit.new_lending_capacity_pct
        return {
            "usd_delta": self.outstanding * pct,
            "pct": pct,
        }

    # ------------------------------------------------------------------
    # 6. Summary
    # ------------------------------------------------------------------

    def summary(self, scenario: Scenario) -> dict[str, Any]:
        """Consolidated stress summary for the private-credit book under a scenario.

        Returns a flat dict of all key metrics.  MTM and realized losses are
        labelled separately to prevent accidental double-counting.
        """
        el = self.expected_loss(scenario)
        mtm = self.mark_to_market_loss(scenario)
        lsi = self.liquidity_stress_score(scenario)
        cap = self.future_lending_capacity_impact(scenario)
        alloc = self.investor_loss_allocation(scenario)
        gating = self.redemption_gating_stress_proxy(scenario)

        return {
            # Exposure
            "outstanding_usd": self.outstanding,
            "net_floating_exposure_usd": self.net_floating_exposure(),
            # Interest
            "base_interest_usd": self.base_interest(),
            "incremental_interest_burden_usd": self.interest_burden(scenario),
            # Credit stress — DISTINCT concepts; do NOT sum
            "stressed_pd": self.stressed_default_probability(scenario),
            "stressed_recovery": self.stressed_recovery(scenario),
            "expected_loss_realized_usd": el,  # realized concept
            "mark_to_market_loss_usd": mtm,    # MTM concept
            # Liquidity / systemic
            "liquidity_stress_score": lsi,
            "redemption_gating_stress_proxy": gating,
            # Lending capacity
            "lending_capacity_usd_delta": cap["usd_delta"],
            "lending_capacity_pct": cap["pct"],
            # Investor allocation (uses max of EL / MTM)
            "investor_loss_allocation_usd": alloc,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_private_credit_book() -> PrivateCreditBook:
    """Build a PrivateCreditBook from YAML base assumptions.

    ALL values are LOW-CONFIDENCE estimates; every assumption is logged.
    Missing YAML keys fall back to model-field defaults and are logged as
    MISSING_DATA.
    """

    def _get(key: str, default: Any, confidence: str = "low") -> Any:
        val = config.assumption_value(key, default)
        if val == default and config.load_base_assumptions().get(key) is None:
            log_missing_data(_logger, what=key, fallback=default)
        else:
            log_assumption_used(_logger, key=key, value=val, confidence=confidence)
        return val

    outstanding = _get(
        "private_credit_ai_datacenter_outstanding", 250_000_000_000.0
    )
    floating_rate_share = _get(
        "private_credit_floating_rate_share", 0.85, confidence="medium"
    )
    hedge_ratio = _get("private_credit_hedge_ratio", 0.20)
    average_spread_bps = _get(
        "private_credit_average_spread_bps", 550.0, confidence="medium"
    )
    average_ltv = _get("private_credit_average_ltv", 0.60)
    average_dscr = _get("private_credit_average_dscr", 1.45)
    default_probability = _get("private_credit_default_probability", 0.03)
    recovery_rate = _get("private_credit_recovery_rate", 0.55)

    raw_investor_base = _get(
        "private_credit_investor_base", dict(_DEFAULT_INVESTOR_BASE)
    )
    # YAML may return the value-field directly (dict); ensure it is a plain dict
    investor_base: dict[str, float]
    if isinstance(raw_investor_base, dict):
        investor_base = {str(k): float(v) for k, v in raw_investor_base.items()}
    else:
        _logger.warning(
            "MISSING_DATA | private_credit_investor_base malformed; using default"
        )
        investor_base = dict(_DEFAULT_INVESTOR_BASE)

    return PrivateCreditBook(
        outstanding=float(outstanding),
        floating_rate_share=float(floating_rate_share),
        hedge_ratio=float(hedge_ratio),
        average_spread_bps=float(average_spread_bps),
        average_ltv=float(average_ltv),
        average_dscr=float(average_dscr),
        default_probability=float(default_probability),
        recovery_rate=float(recovery_rate),
        investor_base=investor_base,
    )
