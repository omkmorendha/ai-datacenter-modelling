"""Stress metrics & breakpoint engine — the integration layer.

Ties the per-module outputs (treasury, financing stack, project, hyperscaler,
private credit, power, contagion) into:

  - a single scenario run (`run_scenario`) producing one consolidated result dict
  - a scenario sweep across all scenarios (`run_all_scenarios`)
  - the BREAKPOINT panel: solves for the shock level at which each danger metric
    crosses its threshold (max SOFR before DSCR breach, max 10Y before refi
    uneconomic, max power price before margin breach, min utilization before
    covenant breach, max credit spread before FCF-after-capex turns negative).

Breakpoints are found by bisection on a single-axis shock applied to a *copy* of
the base scenario, holding everything else at base. They answer "how much room
is left on this one axis" — deliberately one-dimensional; read as sensitivity,
not joint probability.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .. import config
from ..utils.logging import get_logger, log_scenario_result
from .datacenter_project import DataCenterProject, build_representative_project
from .scenarios import Scenario, base_scenario, load_all_scenarios

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Single scenario run
# --------------------------------------------------------------------------- #
def run_scenario(
    scenario: Scenario,
    *,
    project: DataCenterProject | None = None,
    treasury: Any = None,
    private_credit: Any = None,
    hyperscaler: Any = None,
    power: Any = None,
    contagion: Any = None,
    region: str = "PJM",
) -> dict[str, Any]:
    """Run one scenario across all available modules and return a consolidated dict.

    Modules are optional and duck-typed; whatever is passed in is used. The
    project is built from assumptions if not supplied.
    """
    project = project or build_representative_project()

    result: dict[str, Any] = {
        "scenario": scenario.name,
        "label": scenario.label,
        "description": scenario.description,
    }

    pm = project.metrics(scenario)
    result["project"] = pm
    result["financing_stack"] = project.financing_stack.summary(scenario)

    if treasury is not None:
        try:
            result["treasury"] = treasury.summary(scenario)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("treasury.summary failed: %s", exc)

    if private_credit is not None:
        try:
            result["private_credit"] = private_credit.summary(scenario)
        except Exception as exc:  # pragma: no cover
            logger.warning("private_credit.summary failed: %s", exc)

    if hyperscaler is not None:
        try:
            result["hyperscalers"] = hyperscaler.summary(scenario)
        except Exception as exc:  # pragma: no cover
            logger.warning("hyperscaler.summary failed: %s", exc)

    if power is not None:
        try:
            result["power"] = power.summary(scenario, region=region)
        except TypeError:
            try:
                result["power"] = power.summary(scenario)
            except Exception as exc:  # pragma: no cover
                logger.warning("power.summary failed: %s", exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("power.summary failed: %s", exc)

    if contagion is not None:
        try:
            shock = contagion.build_initial_shock_from_scenario(
                scenario, pc_book=private_credit, project=project, hyperscaler=hyperscaler
            )
            result["contagion"] = {
                "first_order": contagion.first_order_loss(shock),
                "propagated": contagion.propagate(shock),
                "funding_contraction": contagion.funding_contraction_estimate(shock),
                "capex_slowdown": contagion.capex_slowdown_estimate(shock),
                "liquidity_stress": contagion.private_market_liquidity_stress_score(shock),
            }
        except Exception as exc:  # pragma: no cover
            logger.warning("contagion failed: %s", exc)

    log_scenario_result(
        logger,
        scenario=scenario.name,
        summary={
            "dscr": round(pm["dscr"], 2),
            "icr": round(pm["icr"], 2),
            "margin": round(pm["ebitda_margin"], 3),
            "fcf": round(pm["fcf_after_capex"], 0),
        },
    )
    return result


def run_all_scenarios(**kwargs) -> dict[str, dict[str, Any]]:
    """Run every scenario in scenarios.yaml; returns name -> result dict."""
    return {name: run_scenario(sc, **kwargs) for name, sc in load_all_scenarios().items()}


# --------------------------------------------------------------------------- #
# Breakpoint engine (bisection on a single shock axis)
# --------------------------------------------------------------------------- #
def _bisect(
    is_safe: Callable[[float], bool],
    lo: float,
    hi: float,
    *,
    tol: float,
    max_iter: int = 60,
    increasing_is_worse: bool = True,
) -> float | None:
    """Find the threshold value x in [lo, hi] where is_safe flips.

    increasing_is_worse=True: is_safe(lo) should be True, is_safe(hi) False;
    returns the largest x still safe. If even lo is unsafe -> lo (already
    breached). If hi is still safe -> None (no breakpoint in range).
    """
    if increasing_is_worse:
        if not is_safe(lo):
            return lo
        if is_safe(hi):
            return None
    else:  # decreasing is worse (e.g. utilization)
        if not is_safe(hi):
            return hi
        if is_safe(lo):
            return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        safe = is_safe(mid)
        if increasing_is_worse:
            if safe:
                lo = mid
            else:
                hi = mid
        else:
            if safe:
                hi = mid
            else:
                lo = mid
        if abs(hi - lo) < tol:
            break
    return 0.5 * (lo + hi)


def compute_breakpoints(
    project: DataCenterProject | None = None, *, base: Scenario | None = None
) -> dict[str, Any]:
    """Compute the five headline breakpoints for the breakpoint panel.

    Each is a one-axis bisection on a copy of the base scenario.
    """
    project = project or build_representative_project()
    base = base or base_scenario()
    thresholds = config.assumption_value("thresholds", {})
    dscr_min = thresholds.get("dscr_min", 1.20)
    icr_min = thresholds.get("icr_min", 1.50)
    margin_min = thresholds.get("ebitda_margin_min", 0.45)

    def with_sofr(bps: float) -> Scenario:
        sc = deepcopy(base)
        sc.rates.sofr_bps += bps
        return sc

    def with_10y(bps: float) -> Scenario:
        sc = deepcopy(base)
        sc.rates.dgs10_bps += bps
        return sc

    def with_power(pct: float) -> Scenario:
        sc = deepcopy(base)
        sc.power.power_price_pct += pct
        return sc

    def with_util(delta: float) -> Scenario:
        sc = deepcopy(base)
        sc.ai_demand.utilization_delta += delta
        return sc

    def with_spread(bps: float) -> Scenario:
        sc = deepcopy(base)
        sc.credit.credit_spread_bps += bps
        return sc

    # 1. max SOFR shock (bps) before DSCR < dscr_min
    max_sofr_bps = _bisect(
        lambda b: project.dscr(with_sofr(b)) >= dscr_min,
        0.0, 1000.0, tol=1.0, increasing_is_worse=True,
    )

    # 2. max 10Y shock (bps) before refinancing uneconomic (proxy: ICR < icr_min)
    max_10y_bps = _bisect(
        lambda b: project.icr(with_10y(b)) >= icr_min,
        0.0, 1000.0, tol=1.0, increasing_is_worse=True,
    )

    # 3. max power price shock (%) before EBITDA margin < margin_min
    max_power_pct = _bisect(
        lambda p: project.ebitda_margin(with_power(p)) >= margin_min,
        0.0, 3.0, tol=0.001, increasing_is_worse=True,
    )

    # 4. min utilization (absolute) before covenant (DSCR) breach
    min_util_delta = _bisect(
        lambda d: project.dscr(with_util(d)) >= dscr_min,
        -0.9, 0.0, tol=0.001, increasing_is_worse=False,
    )
    base_util = project.utilization
    min_util_abs = None if min_util_delta is None else max(0.0, base_util + min_util_delta)

    # 5. max credit spread shock (bps) before FCF-after-capex < 0
    max_spread_bps = _bisect(
        lambda b: project.fcf_after_capex(with_spread(b)) >= 0.0,
        0.0, 2000.0, tol=1.0, increasing_is_worse=True,
    )

    return {
        "max_sofr_shock_bps_before_dscr_breach": max_sofr_bps,
        "max_10y_shock_bps_before_refi_uneconomic": max_10y_bps,
        "max_power_price_pct_before_margin_breach": max_power_pct,
        "min_utilization_before_covenant_breach": min_util_abs,
        "max_credit_spread_bps_before_fcf_negative": max_spread_bps,
        "thresholds_used": {
            "dscr_min": dscr_min,
            "icr_min": icr_min,
            "ebitda_margin_min": margin_min,
        },
        "base_levels": {
            "utilization": base_util,
            "power_cost_per_mwh": project.power_cost_per_mwh,
        },
    }


# --------------------------------------------------------------------------- #
# Sensitivity grid (1-D sweeps for the dashboard sensitivity table)
# --------------------------------------------------------------------------- #
def sensitivity_table(
    project: DataCenterProject | None = None,
    *,
    base: Scenario | None = None,
    axis: str = "sofr_bps",
    values: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Sweep one shock axis and report DSCR/ICR/margin/FCF at each level.

    axis in {sofr_bps, dgs10_bps, credit_spread_bps, power_price_pct,
    utilization_delta, revenue_per_mw_pct}.
    """
    project = project or build_representative_project()
    base = base or base_scenario()
    if values is None:
        values = {
            "sofr_bps": [0, 50, 100, 150, 200, 300],
            "dgs10_bps": [0, 50, 100, 150, 200, 300],
            "credit_spread_bps": [0, 100, 200, 300, 400, 500],
            "power_price_pct": [0.0, 0.1, 0.25, 0.5, 0.75, 1.0],
            "utilization_delta": [0.0, -0.05, -0.1, -0.2, -0.3, -0.4],
            "revenue_per_mw_pct": [0.0, -0.05, -0.1, -0.2, -0.3, -0.4],
        }.get(axis, [0, 50, 100, 150, 200])

    rows: list[dict[str, Any]] = []
    for v in values:
        sc = deepcopy(base)
        if axis in ("sofr_bps", "dgs2_bps", "dgs10_bps", "dgs30_bps", "term_premium_bps"):
            setattr(sc.rates, axis, getattr(sc.rates, axis) + v)
        elif axis == "credit_spread_bps":
            sc.credit.credit_spread_bps += v
        elif axis == "power_price_pct":
            sc.power.power_price_pct += v
        elif axis == "utilization_delta":
            sc.ai_demand.utilization_delta += v
        elif axis == "revenue_per_mw_pct":
            sc.ai_demand.revenue_per_mw_pct += v
        m = project.metrics(sc)
        rows.append(
            {
                axis: v,
                "dscr": m["dscr"],
                "icr": m["icr"],
                "ebitda_margin": m["ebitda_margin"],
                "fcf_after_capex": m["fcf_after_capex"],
                "wacd": project.financing_stack.weighted_average_cost_of_debt(sc),
            }
        )
    return rows


def refinancing_gap(
    project: DataCenterProject | None = None, *, scenario: Scenario | None = None
) -> dict[int, dict[str, float]]:
    """Per-year refinancing wall vs the scenario-adjusted refinancing probability.

    'gap' = principal at risk = maturing principal * (1 - refi probability) under
    the scenario. A crude but useful refinancing-risk signal.
    """
    project = project or build_representative_project()
    scenario = scenario or base_scenario()
    base_refi = config.assumption_value("base_refinancing_probability", 0.90)
    refi_prob = max(0.0, min(1.0, base_refi + scenario.credit.refinancing_probability_delta))
    wall = project.financing_stack.refinancing_wall()
    out: dict[int, dict[str, float]] = {}
    for year, principal in wall.items():
        out[year] = {
            "maturing_principal": principal,
            "refi_probability": refi_prob,
            "principal_at_risk": principal * (1.0 - refi_prob),
        }
    return out
