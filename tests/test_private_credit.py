"""Tests for the private-credit module: expected loss, MTM, net floating, allocation.

Tests the contract built by the module-build workflow. All private-credit
numbers are LOW-CONFIDENCE assumptions; here we test the *math*, not the levels.
"""

from __future__ import annotations

import math

import pytest

from src.model.scenarios import load_scenario

pc = pytest.importorskip(
    "src.model.private_credit_module",
    reason="private_credit_module not built yet",
)


def book():
    return pc.build_private_credit_book()


# --------------------------------------------------------------------------- #
# Net floating exposure (gross vs net)
# --------------------------------------------------------------------------- #
def test_net_floating_exposure_below_gross():
    b = book()
    net = b.net_floating_exposure()
    gross = b.outstanding * b.floating_rate_share
    # net (after hedges) <= gross floating
    assert 0 <= net <= gross + 1e-6


# --------------------------------------------------------------------------- #
# Expected loss
# --------------------------------------------------------------------------- #
def test_expected_loss_zero_when_no_default_shock_and_full_recovery(monkeypatch):
    b = book()
    b.default_probability = 0.0
    el = b.expected_loss(load_scenario("base_case"))
    assert math.isclose(el, 0.0, abs_tol=1e-6)


def test_expected_loss_increases_with_default_multiplier():
    b = book()
    base = load_scenario("base_case")
    severe = load_scenario("severe_private_credit_liquidity_crunch")  # PD x2, recovery down
    assert b.expected_loss(severe) > b.expected_loss(base)


def test_expected_loss_formula():
    b = book()
    base = load_scenario("base_case")
    pd = b.stressed_default_probability(base)
    rec = b.stressed_recovery(base)
    expected = b.outstanding * pd * (1 - rec)
    assert math.isclose(b.expected_loss(base), expected, rel_tol=1e-6)


# --------------------------------------------------------------------------- #
# Mark-to-market vs realized
# --------------------------------------------------------------------------- #
def test_mtm_loss_tracks_nav_haircut():
    b = book()
    severe = load_scenario("severe_private_credit_liquidity_crunch")  # 15% NAV haircut
    expected = b.outstanding * severe.private_credit.nav_haircut
    assert math.isclose(b.mark_to_market_loss(severe), expected, rel_tol=1e-6)


def test_mtm_distinct_from_expected_loss():
    b = book()
    severe = load_scenario("severe_private_credit_liquidity_crunch")
    # MTM and realized expected loss are different concepts -> generally different values
    assert b.mark_to_market_loss(severe) != b.expected_loss(severe)


# --------------------------------------------------------------------------- #
# Interest burden under SOFR shock
# --------------------------------------------------------------------------- #
def test_interest_burden_scales_with_sofr_shock():
    b = book()
    base = load_scenario("base_case")
    crunch = load_scenario("severe_private_credit_liquidity_crunch")  # +150 SOFR
    assert b.interest_burden(crunch) > b.interest_burden(base)
    assert math.isclose(b.interest_burden(base), 0.0, abs_tol=1e-6)  # no SOFR shock in base


# --------------------------------------------------------------------------- #
# Investor loss allocation
# --------------------------------------------------------------------------- #
def test_investor_loss_allocation_sums_to_total_loss():
    b = book()
    severe = load_scenario("severe_private_credit_liquidity_crunch")
    alloc = b.investor_loss_allocation(severe)
    total = max(b.expected_loss(severe), b.mark_to_market_loss(severe))
    assert math.isclose(sum(alloc.values()), total, rel_tol=1e-3)


def test_liquidity_stress_in_unit_interval():
    b = book()
    for name in ("base_case", "high_for_longer", "combined_severe_case"):
        s = b.liquidity_stress_score(load_scenario(name))
        assert 0.0 <= s <= 1.0
