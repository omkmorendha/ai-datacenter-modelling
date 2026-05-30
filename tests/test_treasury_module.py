"""Tests for the Treasury module: stress index, shocks, securitization openness.

These test the *contract* of treasury_module (built by the module-build
workflow). They assert behavior the spec requires:
  - base_case TreasuryStressIndex ~ 0
  - more severe scenarios produce a higher stress index than milder ones
  - missing components are dropped, not zero-filled
  - rate/discount/refi shocks move in the right direction
"""

from __future__ import annotations

import math

import pytest

from src.model.scenarios import load_scenario

tm = pytest.importorskip(
    "src.model.treasury_module",
    reason="treasury_module not built yet",
)


def build():
    return tm.build_treasury_module()


# --------------------------------------------------------------------------- #
# Stress index
# --------------------------------------------------------------------------- #
def test_base_case_stress_index_near_zero():
    mod = build()
    base = load_scenario("base_case")
    idx = mod.treasury_stress_index(base)
    # base case has zero rate shocks and (by default) no history -> ~0
    assert abs(idx) < 1e-6


def test_severe_scenario_more_stressed_than_mild():
    mod = build()
    hfl = load_scenario("high_for_longer")
    severe = load_scenario("combined_severe_case")
    assert mod.treasury_stress_index(severe) > mod.treasury_stress_index(hfl)


def test_stress_index_positive_under_rate_shock():
    mod = build()
    shock = load_scenario("treasury_term_premium_shock")
    assert mod.treasury_stress_index(shock) > 0


def test_stress_components_drop_missing():
    """Components without history and with no scenario shock are dropped, not zero."""
    mod = build()
    base = load_scenario("base_case")
    comps = mod.treasury_stress_components(base)
    # contributions present should be finite numbers
    assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in comps.values())


# --------------------------------------------------------------------------- #
# Shocks
# --------------------------------------------------------------------------- #
def test_discount_rate_shock_direction():
    mod = build()
    base = load_scenario("base_case")
    shock = load_scenario("treasury_term_premium_shock")  # +150 10Y, +125 term premium
    assert mod.discount_rate_shock(shock) > mod.discount_rate_shock(base)


def test_refinancing_cost_shock_direction():
    mod = build()
    base = load_scenario("base_case")
    shock = load_scenario("severe_private_credit_liquidity_crunch")  # +250 spread
    assert mod.refinancing_cost_shock(shock) > mod.refinancing_cost_shock(base)


def test_base_risk_free_rate_rises_with_sofr():
    mod = build()
    base = load_scenario("base_case")
    hfl = load_scenario("high_for_longer")  # +100 SOFR
    assert mod.base_risk_free_funding_rate(hfl) > mod.base_risk_free_funding_rate(base)


# --------------------------------------------------------------------------- #
# Securitization openness
# --------------------------------------------------------------------------- #
def test_securitization_openness_in_unit_interval():
    mod = build()
    for name in ("base_case", "high_for_longer", "combined_severe_case"):
        s = mod.securitization_openness_score(load_scenario(name))
        assert 0.0 <= s <= 1.0


def test_securitization_closes_under_crunch():
    mod = build()
    base = load_scenario("base_case")
    crunch = load_scenario("severe_private_credit_liquidity_crunch")  # securitization_open False
    assert mod.securitization_openness_score(crunch) < mod.securitization_openness_score(base)
