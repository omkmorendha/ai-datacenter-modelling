"""Power module: regional electricity pricing, grid constraints, and power-shock analysis.

This module encapsulates all power-related computations for AI datacenter stress
modelling. It reads three base-assumption keys:
  - power_price_base_by_region   : ISO/RTO -> $/MWh (wholesale all-in delivered)
  - grid_capacity_constraint_score_by_region : ISO/RTO -> score 0..1
  - datacenter_load_growth_rate  : scalar annual ratio

Important distinction: datacenter power demand (the focus here) is a subset of
total commercial electricity demand and should NOT be conflated with total grid
load. Datacenters typically represent 1-3% of national electricity consumption
but can constitute 10-20% of regional peak demand in constrained regions like
Northern Virginia (PJM). The constraint scores here reflect DC-specific
interconnection queue pressure, not aggregate grid utilisation.

Spec nuances honoured:
  - power_price_pct shock is MULTIPLICATIVE (0.10 = +10% on base price).
  - margin_compression is a FIRST-ORDER approximation assuming a fixed
    baseline_power_share_of_revenue; it ignores second-order revenue offsets
    and assumes no pass-through clauses in offtake agreements.
  - capex_delay_probability is an engineering heuristic, not a regression;
    treat as directional signal only.
  - All private-credit-adjacent estimates carry LOW-CONFIDENCE in any
    downstream consumer.
"""

from __future__ import annotations

from .. import config
from ..utils.logging import get_logger, log_assumption_used, log_missing_data
from ..utils.validation import clamp
from .scenarios import Scenario

_logger = get_logger("ai_dc_model.power_module")

# Default fallback values when assumption keys are absent.
_DEFAULT_POWER_PRICE = 55.0   # $/MWh – rough US average for reference
_DEFAULT_CONSTRAINT = 0.5     # mid-range when region is unknown
_DEFAULT_LOAD_GROWTH = 0.20   # 20% pa – spec default

# Shock levels for the spot-stress convenience dict.
_SPOT_SHOCKS: dict[str, float] = {
    "+10%": 0.10,
    "+25%": 0.25,
    "+50%": 0.50,
    "+100%": 1.00,
}


class PowerModule:
    """Service object encapsulating power-market analysis for AI datacenter stress testing.

    This is a plain class (not pydantic) because it holds mutable cached state
    (the assumption dicts loaded once at construction time).

    Parameters
    ----------
    price_by_region:
        ISO/RTO -> base $/MWh mapping read from config at construction time.
    constraint_by_region:
        ISO/RTO -> grid-constraint severity score (0 = ample, 1 = severe).
    load_growth_rate:
        Annual DC electricity-load growth rate (scalar); used in
        capex_delay_probability.
    """

    def __init__(
        self,
        price_by_region: dict[str, float],
        constraint_by_region: dict[str, float],
        load_growth_rate: float,
    ) -> None:
        self._price_by_region: dict[str, float] = price_by_region
        self._constraint_by_region: dict[str, float] = constraint_by_region
        self._load_growth_rate: float = load_growth_rate

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _base_price(self, region: str) -> float:
        """Return the base $/MWh for *region*, with fallback to mean + warning."""
        if region in self._price_by_region:
            return self._price_by_region[region]
        known = list(self._price_by_region.values())
        fallback = float(sum(known) / len(known)) if known else _DEFAULT_POWER_PRICE
        log_missing_data(
            _logger,
            what=f"power_price_base_by_region[{region!r}]",
            fallback=round(fallback, 2),
        )
        return fallback

    def _constraint_score(self, region: str) -> float:
        """Return the grid-constraint score for *region*, with fallback + warning."""
        if region in self._constraint_by_region:
            return self._constraint_by_region[region]
        log_missing_data(
            _logger,
            what=f"grid_capacity_constraint_score_by_region[{region!r}]",
            fallback=_DEFAULT_CONSTRAINT,
        )
        return _DEFAULT_CONSTRAINT

    @staticmethod
    def _power_price_pct(scenario: Scenario | None) -> float:
        """Extract the multiplicative power-price shock from a scenario (or 0.0)."""
        if scenario is None:
            return 0.0
        return scenario.power.power_price_pct

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def power_cost_per_mwh(
        self,
        region: str,
        scenario: Scenario | None = None,
    ) -> float:
        """Shocked power cost in $/MWh for *region* under *scenario*.

        Applies the multiplicative power_price_pct shock from the scenario.
        An unrecognised region falls back to the mean of known regions plus a
        MISSING_DATA log entry.

        Parameters
        ----------
        region:
            ISO/RTO identifier, e.g. 'PJM', 'ERCOT', 'CAISO'.
        scenario:
            Optional Scenario; None -> base (zero-shock) price.

        Returns
        -------
        float
            $/MWh after applying scenario shock.
        """
        base = self._base_price(region)
        pct = self._power_price_pct(scenario)
        return base * (1.0 + pct)

    def power_cost_shock_scenarios(self, region: str) -> dict[str, float]:
        """Return spot-stress power costs for *region* at fixed shock levels.

        Provides a quick reference across four additive percentage shocks applied
        to the base (zero-shock) price. These are scenario-independent convenience
        outputs; for scenario-specific pricing use power_cost_per_mwh().

        Note: DC power demand != total commercial electricity demand; these shocks
        reflect DC-specific procurement costs which may diverge from retail rates.

        Returns
        -------
        dict
            Keys '+10%', '+25%', '+50%', '+100%' -> $/MWh.
        """
        base = self._base_price(region)
        return {label: base * (1.0 + shock) for label, shock in _SPOT_SHOCKS.items()}

    def regional_margin_compression(
        self,
        region: str,
        scenario: Scenario | None,
        *,
        baseline_power_share_of_revenue: float = 0.30,
    ) -> float:
        """Estimate margin compression (decimal pp) due to power-price shock.

        **First-order approximation**: assumes power costs are a fixed
        *baseline_power_share_of_revenue* fraction of revenue, no pass-through
        clauses, and no second-order effects (e.g. demand elasticity, mix shifts).
        For a DC operator with 30% power-cost/revenue share, a +25% power shock
        -> -7.5 pp margin compression.

        Formula:
            compression_pp = baseline_power_share_of_revenue * power_price_pct

        This is a directional approximation; actual compression depends on
        contract structure, hedging, and whether the operator has fixed-price PPAs.

        Parameters
        ----------
        region:
            ISO/RTO identifier (used only to validate the region exists; the
            power_price_pct shock is region-agnostic in the current spec).
        scenario:
            Scenario providing power_price_pct shock.
        baseline_power_share_of_revenue:
            Share of revenue consumed by power in the base case (default 0.30).

        Returns
        -------
        float
            Margin compression in decimal percentage points (positive = worse).
        """
        # Validate region exists (logs warning if unknown)
        self._base_price(region)
        pct = self._power_price_pct(scenario)
        return baseline_power_share_of_revenue * pct

    def power_availability_constraint(self, region: str) -> float:
        """Grid-constraint severity score for *region* (0 = ample, 1 = severe).

        Reads from grid_capacity_constraint_score_by_region assumption. Defaults
        to 0.5 for unknown regions, logged as MISSING_DATA.

        Note: score reflects DC-specific interconnection queue congestion, not
        aggregate grid capacity utilisation.
        """
        return self._constraint_score(region)

    def capex_delay_probability(
        self,
        region: str,
        scenario: Scenario | None = None,
    ) -> float:
        """Estimate probability of capex delay due to grid / power constraints.

        Engineering heuristic combining three independent risk drivers:

            p = clamp(
                  0.30 * constraint_score
                + 0.50 * min(load_growth_rate, 1.0)
                + 0.20 * max(0, power_price_pct),
                0, 1
            )

        Weights are qualitative:
          - Grid constraint (0.30): interconnection-queue bottleneck risk.
          - Load growth (0.50): demand outpacing supply increases queuing delays.
          - Power-price shock (0.20): high prices may signal tight capacity;
            only upward shocks are penalised (max(0, pct)).

        This is directional only; it is NOT a calibrated probability model.
        DC load growth is measured relative to DC-specific demand, not total
        commercial electricity demand.

        Returns
        -------
        float
            Estimated probability in [0, 1].
        """
        constraint = self._constraint_score(region)
        load_growth = self._load_growth_rate
        pct = self._power_price_pct(scenario)
        raw = (
            0.30 * constraint
            + 0.50 * min(load_growth, 1.0)
            + 0.20 * max(0.0, pct)
        )
        return clamp(raw, 0.0, 1.0)

    def regional_summary(
        self,
        scenario: Scenario | None = None,
    ) -> dict[str, dict[str, float]]:
        """Return a per-region summary dict for all known regions.

        Each region entry contains:
          - price          : base $/MWh (no shock)
          - shocked_price  : $/MWh under scenario
          - constraint     : grid-constraint score
          - capex_delay_prob: capex delay probability
          - margin_compression: decimal pp margin compression (30% power share basis)

        Parameters
        ----------
        scenario:
            Optional Scenario; None -> base (zero-shock) values.

        Returns
        -------
        dict
            region -> {price, shocked_price, constraint, capex_delay_prob,
                       margin_compression}
        """
        all_regions = sorted(
            set(self._price_by_region) | set(self._constraint_by_region)
        )
        result: dict[str, dict[str, float]] = {}
        for region in all_regions:
            price = self._base_price(region)
            shocked = self.power_cost_per_mwh(region, scenario)
            constraint = self.power_availability_constraint(region)
            delay_prob = self.capex_delay_probability(region, scenario)
            compression = self.regional_margin_compression(region, scenario)
            result[region] = {
                "price": price,
                "shocked_price": shocked,
                "constraint": constraint,
                "capex_delay_prob": delay_prob,
                "margin_compression": compression,
            }
        return result

    def summary(
        self,
        scenario: Scenario | None = None,
        region: str | None = None,
    ) -> dict:
        """High-level summary for a single region (default 'PJM').

        Convenience wrapper combining all per-region metrics into a flat dict
        plus a 'spot_shocks' sub-dict from power_cost_shock_scenarios().

        Parameters
        ----------
        scenario:
            Optional Scenario; None -> base values.
        region:
            ISO/RTO identifier; defaults to 'PJM'.

        Returns
        -------
        dict
            Flat dict with keys: region, base_price, shocked_price, constraint,
            capex_delay_prob, margin_compression, spot_shocks.
        """
        if region is None:
            region = "PJM"
        base_price = self._base_price(region)
        shocked_price = self.power_cost_per_mwh(region, scenario)
        constraint = self.power_availability_constraint(region)
        delay_prob = self.capex_delay_probability(region, scenario)
        compression = self.regional_margin_compression(region, scenario)
        spot_shocks = self.power_cost_shock_scenarios(region)
        return {
            "region": region,
            "base_price": base_price,
            "shocked_price": shocked_price,
            "constraint": constraint,
            "capex_delay_prob": delay_prob,
            "margin_compression": compression,
            "spot_shocks": spot_shocks,
        }


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def build_power_module() -> PowerModule:
    """Construct a PowerModule from base_assumptions.yaml.

    Reads:
      - power_price_base_by_region
      - grid_capacity_constraint_score_by_region
      - datacenter_load_growth_rate

    Falls back to sensible defaults and logs any missing assumptions.
    """
    price_raw = config.assumption_value("power_price_base_by_region")
    if price_raw and isinstance(price_raw, dict):
        price_by_region: dict[str, float] = {
            k: float(v) for k, v in price_raw.items()
        }
        log_assumption_used(
            _logger,
            key="power_price_base_by_region",
            value=price_by_region,
            confidence="medium",
        )
    else:
        log_missing_data(
            _logger,
            what="power_price_base_by_region",
            fallback={"PJM": _DEFAULT_POWER_PRICE},
        )
        price_by_region = {"PJM": _DEFAULT_POWER_PRICE}

    constraint_raw = config.assumption_value("grid_capacity_constraint_score_by_region")
    if constraint_raw and isinstance(constraint_raw, dict):
        constraint_by_region: dict[str, float] = {
            k: float(v) for k, v in constraint_raw.items()
        }
        log_assumption_used(
            _logger,
            key="grid_capacity_constraint_score_by_region",
            value=constraint_by_region,
            confidence="low",
        )
    else:
        log_missing_data(
            _logger,
            what="grid_capacity_constraint_score_by_region",
            fallback={"PJM": _DEFAULT_CONSTRAINT},
        )
        constraint_by_region = {"PJM": _DEFAULT_CONSTRAINT}

    load_growth = config.assumption_value("datacenter_load_growth_rate")
    if load_growth is not None:
        load_growth = float(load_growth)
        log_assumption_used(
            _logger,
            key="datacenter_load_growth_rate",
            value=load_growth,
            confidence="low",
        )
    else:
        log_missing_data(
            _logger,
            what="datacenter_load_growth_rate",
            fallback=_DEFAULT_LOAD_GROWTH,
        )
        load_growth = _DEFAULT_LOAD_GROWTH

    return PowerModule(
        price_by_region=price_by_region,
        constraint_by_region=constraint_by_region,
        load_growth_rate=load_growth,
    )
