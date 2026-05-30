"""Financing-stack module: the mixed capital structure of an AI/DC build.

This is the heart of the thesis that the buildout is *not* uniformly
floating-rate private credit. Each :class:`FinancingSource` carries its own
rate reference, fixed/floating flag, hedge ratio, swap ratio, spread, maturity,
covenants and lender type. The :class:`FinancingStack` aggregates them and
applies scenario shocks to produce weighted cost of debt, net floating
exposure, interest expense, refinancing walls, spread-shock and mark-to-market
impacts, and covenant-breach flags.

Key distinctions the spec insists on:
  - floating exposure vs *net* floating exposure after hedges/swaps
  - committed vs drawn is represented by `amount` being drawn debt
  - fixed-coupon tranches are immune to rate shocks (only refi reprices them)
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from ..utils.validation import bps_to_decimal, safe_div
from .scenarios import Scenario


class RateType(StrEnum):
    FIXED = "fixed"
    FLOATING = "floating"


class LenderType(StrEnum):
    BANK = "bank"
    PRIVATE_CREDIT = "private_credit"
    BOND_MARKET = "bond_market"
    SECURITIZATION = "securitization"
    INSURER = "insurer"
    PENSION = "pension"
    HYPERSCALER = "hyperscaler"
    OTHER = "other"


class AmortizationType(StrEnum):
    BULLET = "bullet"          # principal due at maturity (refi wall)
    AMORTIZING = "amortizing"  # principal paid down over life
    INTEREST_ONLY = "interest_only"


class FinancingSource(BaseModel):
    """One tranche / source in the capital stack."""

    name: str
    amount: float = Field(ge=0)                      # drawn debt, USD
    fixed_or_floating: RateType = RateType.FLOATING
    base_rate_reference: str = "sofr"                # sofr|2y|10y|30y|fixed|custom
    base_rate: float = 0.0                           # current level of reference (decimal)
    credit_spread_bps: float = 0.0
    fixed_coupon: float = 0.0                        # used when reference == 'fixed'
    maturity_year: int = 2030
    amortization_type: AmortizationType = AmortizationType.BULLET
    hedge_ratio: float = Field(default=0.0, ge=0, le=1)
    swapped_to_fixed_ratio: float = Field(default=0.0, ge=0, le=1)
    covenant_dscr_min: float = 1.20
    covenant_icr_min: float = 1.50
    refinancing_probability: float = Field(default=0.90, ge=0, le=1)
    lender_type: LenderType = LenderType.OTHER
    mark_to_market_sensitivity: float = 0.0          # price decline per +100bp yield (decimal)

    @field_validator("base_rate_reference")
    @classmethod
    def _norm_ref(cls, v: str) -> str:
        return v.lower().strip()

    # ------------------------------------------------------------------ #
    @property
    def effective_floating_fraction(self) -> float:
        """Fraction of this tranche that actually floats after hedges/swaps.

        Fixed tranches have zero. Floating tranches reduce by hedge+swap, but a
        single dollar can't be hedged twice, so we cap the protected portion.
        """
        if self.fixed_or_floating == RateType.FIXED:
            return 0.0
        protected = min(1.0, self.hedge_ratio + self.swapped_to_fixed_ratio)
        return 1.0 - protected

    def all_in_rate(self, scenario: Scenario | None = None) -> float:
        """Current all-in coupon (decimal), applying a scenario's rate+spread shocks.

        Only the *net floating* portion reprices with the rate shock; the
        hedged/swapped/fixed portion keeps its base rate. Spread shocks apply to
        the whole tranche as an ongoing all-in-cost proxy.
        """
        spread = bps_to_decimal(self.credit_spread_bps)
        if self.fixed_or_floating == RateType.FIXED or self.base_rate_reference == "fixed":
            base = self.fixed_coupon if self.fixed_coupon else self.base_rate
            # fixed coupons don't reprice on rate moves; spread shock only bites on refi.
            return base + spread

        rate_shock = 0.0
        spread_shock = 0.0
        if scenario is not None:
            rate_shock = bps_to_decimal(scenario.rates.reference_bps(self.base_rate_reference))
            spread_shock = bps_to_decimal(scenario.credit.credit_spread_bps)

        floating_frac = self.effective_floating_fraction
        # Net floating portion sees the rate shock; the rest is protected.
        shocked_base = self.base_rate + floating_frac * rate_shock
        return shocked_base + spread + spread_shock

    def annual_interest(self, scenario: Scenario | None = None) -> float:
        return self.amount * self.all_in_rate(scenario)

    def net_floating_exposure(self) -> float:
        """Dollar amount that reprices with floating rates after hedges/swaps."""
        return self.amount * self.effective_floating_fraction

    def mark_to_market_loss(self, scenario: Scenario | None = None) -> float:
        """Estimated MTM loss from the scenario's representative yield move.

        Uses the tranche's mark_to_market_sensitivity (price decline per +100bp)
        applied to the relevant reference rate shock plus the credit-spread shock.
        This is *mark-to-market* stress, NOT realized default loss.
        """
        if scenario is None or self.mark_to_market_sensitivity == 0:
            return 0.0
        rate_move_bps = abs(scenario.rates.reference_bps(self.base_rate_reference))
        spread_move_bps = abs(scenario.credit.credit_spread_bps)
        total_move_units = (rate_move_bps + spread_move_bps) / 100.0  # in "100bp" units
        price_decline = self.mark_to_market_sensitivity * total_move_units
        return self.amount * price_decline


class FinancingStack(BaseModel):
    """A collection of financing sources for one borrower/project."""

    sources: list[FinancingSource] = Field(default_factory=list)

    # ---- aggregates --------------------------------------------------------
    @property
    def total_debt(self) -> float:
        return sum(s.amount for s in self.sources)

    def shares(self) -> dict[str, float]:
        total = self.total_debt
        if total == 0:
            return {s.name: 0.0 for s in self.sources}
        return {s.name: s.amount / total for s in self.sources}

    def weighted_average_cost_of_debt(self, scenario: Scenario | None = None) -> float:
        """Amount-weighted all-in cost of debt (decimal)."""
        total = self.total_debt
        if total == 0:
            return 0.0
        return sum(s.amount * s.all_in_rate(scenario) for s in self.sources) / total

    def annual_interest_expense(self, scenario: Scenario | None = None) -> float:
        return sum(s.annual_interest(scenario) for s in self.sources)

    def floating_rate_exposure(self, *, net_of_hedges: bool = True) -> float:
        """Total floating-rate dollar exposure (net of hedges by default)."""
        if net_of_hedges:
            return sum(s.net_floating_exposure() for s in self.sources)
        return sum(
            s.amount for s in self.sources if s.fixed_or_floating == RateType.FLOATING
        )

    def floating_rate_share(self, *, net_of_hedges: bool = True) -> float:
        return safe_div(
            self.floating_rate_exposure(net_of_hedges=net_of_hedges),
            self.total_debt,
            default=0.0,
        )

    def refinancing_wall(self) -> dict[int, float]:
        """Principal coming due by maturity year (bullets + IO; amortizing roughly
        bullets the residual at maturity in this v0)."""
        wall: dict[int, float] = {}
        for s in self.sources:
            wall[s.maturity_year] = wall.get(s.maturity_year, 0.0) + s.amount
        return dict(sorted(wall.items()))

    def spread_shock_impact(self, scenario: Scenario) -> float:
        """Incremental annual interest from the scenario's credit-spread shock alone."""
        base = self.annual_interest_expense(scenario=None)
        shocked = self.annual_interest_expense(scenario=scenario)
        return shocked - base

    def mark_to_market_loss(self, scenario: Scenario) -> float:
        return sum(s.mark_to_market_loss(scenario) for s in self.sources)

    def covenant_breach_flags(
        self, *, dscr: float, icr: float
    ) -> dict[str, dict[str, bool]]:
        """Per-tranche covenant breach flags given project-level DSCR/ICR."""
        flags: dict[str, dict[str, bool]] = {}
        for s in self.sources:
            flags[s.name] = {
                "dscr_breach": dscr < s.covenant_dscr_min,
                "icr_breach": icr < s.covenant_icr_min,
            }
        return flags

    def exposure_by_lender_type(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in self.sources:
            out[s.lender_type.value] = out.get(s.lender_type.value, 0.0) + s.amount
        return out

    def summary(self, scenario: Scenario | None = None) -> dict:
        return {
            "total_debt": self.total_debt,
            "wacd": self.weighted_average_cost_of_debt(scenario),
            "annual_interest_expense": self.annual_interest_expense(scenario),
            "floating_exposure_net": self.floating_rate_exposure(net_of_hedges=True),
            "floating_share_net": self.floating_rate_share(net_of_hedges=True),
            "floating_share_gross": self.floating_rate_share(net_of_hedges=False),
            "refinancing_wall": self.refinancing_wall(),
            "mtm_loss": self.mark_to_market_loss(scenario) if scenario else 0.0,
            "exposure_by_lender_type": self.exposure_by_lender_type(),
        }


# --------------------------------------------------------------------------- #
# Factory: build a representative stack from base assumptions.
# --------------------------------------------------------------------------- #
def build_representative_stack(
    total_debt: float,
    *,
    base_rates: dict[str, float] | None = None,
    base_spread_bps: float = 200.0,
    maturity_distribution: dict[int, float] | None = None,
) -> FinancingStack:
    """Construct a representative mixed financing stack.

    Uses the representative_financing_stack_shares assumption to split
    ``total_debt`` across sources, assigning sensible rate references, fixed/
    floating flags, hedge ratios and lender types per source. ``base_rates`` maps
    reference labels ('sofr','10y',...) to current decimal levels (from the
    Treasury module / FRED); defaults to rough levels if missing.
    """
    from .. import config

    base_rates = base_rates or {
        "sofr": 0.0433,
        "2y": 0.043,
        "10y": 0.045,
        "30y": 0.047,
    }
    shares = config.assumption_value("representative_financing_stack_shares", {})
    maturity_distribution = maturity_distribution or config.assumption_value(
        "refinancing_maturity_distribution", {2030: 1.0}
    )

    # Profile per source:
    # (rate_type, reference, hedge, swap, lender, mtm_sens, spread_add_bps, amort)
    profiles: dict[str, tuple] = {
        "hyperscaler_retained_earnings": (
            RateType.FIXED, "fixed", 0.0, 0.0, LenderType.HYPERSCALER, 0.0, -50, AmortizationType.INTEREST_ONLY),
        "corporate_bonds": (
            RateType.FIXED, "fixed", 0.0, 0.0, LenderType.BOND_MARKET, 0.07, 0, AmortizationType.BULLET),
        "bank_credit_facility": (
            RateType.FLOATING, "sofr", 0.10, 0.10, LenderType.BANK, 0.02, 25, AmortizationType.BULLET),
        "syndicated_loans": (
            RateType.FLOATING, "sofr", 0.15, 0.20, LenderType.BANK, 0.03, 75, AmortizationType.AMORTIZING),
        "private_credit": (
            RateType.FLOATING, "sofr", 0.15, 0.05, LenderType.PRIVATE_CREDIT, 0.04, 350, AmortizationType.BULLET),
        "abs_cmbs_securitization": (
            RateType.FIXED, "fixed", 0.0, 0.0, LenderType.SECURITIZATION, 0.06, 150, AmortizationType.AMORTIZING),
        "project_finance_spv": (
            RateType.FLOATING, "sofr", 0.40, 0.30, LenderType.BANK, 0.05, 225, AmortizationType.AMORTIZING),
        "lease_obligations": (
            RateType.FIXED, "fixed", 0.0, 0.0, LenderType.OTHER, 0.03, 100, AmortizationType.AMORTIZING),
        "equity_jv": (
            RateType.FIXED, "fixed", 0.0, 0.0, LenderType.OTHER, 0.0, 0, AmortizationType.INTEREST_ONLY),
    }

    # Spread maturities across the wall years (cycle through declared buckets).
    wall_years = sorted(maturity_distribution) if maturity_distribution else [2030]

    sources: list[FinancingSource] = []
    for i, (name, share) in enumerate(shares.items()):
        if name not in profiles:
            continue
        rate_type, ref, hedge, swap, lender, mtm, spread_add, amort = profiles[name]
        amount = total_debt * float(share)
        if amount <= 0:
            continue
        if ref == "fixed":
            base_rate = base_rates.get("10y", 0.045)
            fixed_coupon = base_rate + bps_to_decimal(base_spread_bps + spread_add)
        else:
            base_rate = base_rates.get(ref, base_rates.get("sofr", 0.0433))
            fixed_coupon = 0.0
        maturity = wall_years[i % len(wall_years)]
        sources.append(
            FinancingSource(
                name=name,
                amount=amount,
                fixed_or_floating=rate_type,
                base_rate_reference=ref,
                base_rate=base_rate,
                credit_spread_bps=base_spread_bps + spread_add,
                fixed_coupon=fixed_coupon,
                maturity_year=int(maturity),
                amortization_type=amort,
                hedge_ratio=hedge,
                swapped_to_fixed_ratio=swap,
                lender_type=lender,
                mark_to_market_sensitivity=mtm,
            )
        )
    return FinancingStack(sources=sources)
