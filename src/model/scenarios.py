"""Scenario definitions and the shock vocabulary the whole model speaks.

A :class:`Scenario` is a typed bundle of shocks (rates, credit, power, AI
demand, private credit, equity, inflation, capex) expressed relative to the
base case. Every model module accepts a ``Scenario`` and applies the relevant
sub-shock. This keeps the shock vocabulary in exactly one place.

Units follow scenarios.yaml:
  *_bps   additive basis points  -> use bps_to_decimal helpers
  *_pct   multiplicative percent  (0.10 = +10%)
  *_delta additive delta in native units
  *_mult  multiplier on a base quantity
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import config
from ..utils.validation import bps_to_decimal


class RateShocks(BaseModel):
    sofr_bps: float = 0.0
    dgs2_bps: float = 0.0
    dgs10_bps: float = 0.0
    dgs30_bps: float = 0.0
    term_premium_bps: float = 0.0

    def reference_bps(self, reference: str) -> float:
        """Basis-point shock for a given base-rate reference label."""
        return {
            "sofr": self.sofr_bps,
            "2y": self.dgs2_bps,
            "dgs2": self.dgs2_bps,
            "10y": self.dgs10_bps,
            "dgs10": self.dgs10_bps,
            "30y": self.dgs30_bps,
            "dgs30": self.dgs30_bps,
            "fixed": 0.0,
            "fixed_coupon": 0.0,
            "custom": 0.0,
        }.get(reference.lower(), 0.0)


class CreditShocks(BaseModel):
    credit_spread_bps: float = 0.0
    refinancing_probability_delta: float = 0.0
    securitization_open: bool = True
    securitization_haircut_delta: float = 0.0
    cap_rate_bps: float = 0.0


class PowerShocks(BaseModel):
    power_price_pct: float = 0.0


class AIDemandShocks(BaseModel):
    utilization_delta: float = 0.0
    revenue_per_mw_pct: float = 0.0


class PrivateCreditShocks(BaseModel):
    nav_haircut: float = 0.0
    default_probability_mult: float = 1.0
    recovery_rate_delta: float = 0.0
    new_lending_capacity_pct: float = 0.0


class EquityShocks(BaseModel):
    valuation_haircut: float = 0.0


class InflationShocks(BaseModel):
    inflation_expectations_bps: float = 0.0


class CapexShocks(BaseModel):
    capex_cut_pct: float = 0.0


class Scenario(BaseModel):
    name: str
    label: str = ""
    description: str = ""
    rates: RateShocks = Field(default_factory=RateShocks)
    credit: CreditShocks = Field(default_factory=CreditShocks)
    power: PowerShocks = Field(default_factory=PowerShocks)
    ai_demand: AIDemandShocks = Field(default_factory=AIDemandShocks)
    private_credit: PrivateCreditShocks = Field(default_factory=PrivateCreditShocks)
    equity: EquityShocks = Field(default_factory=EquityShocks)
    inflation: InflationShocks = Field(default_factory=InflationShocks)
    capex: CapexShocks = Field(default_factory=CapexShocks)

    # ---- convenience accessors --------------------------------------------
    @property
    def credit_spread_decimal(self) -> float:
        return bps_to_decimal(self.credit.credit_spread_bps)

    def is_base(self) -> bool:
        return self.name == "base_case"

    # ---- construction ------------------------------------------------------
    @classmethod
    def from_dict(cls, name: str, raw: dict) -> "Scenario":
        """Build a Scenario from a scenarios.yaml entry (lenient to missing groups)."""
        return cls(
            name=name,
            label=raw.get("label", name),
            description=raw.get("description", ""),
            rates=RateShocks(**raw.get("rates", {})),
            credit=CreditShocks(**raw.get("credit", {})),
            power=PowerShocks(**raw.get("power", {})),
            ai_demand=AIDemandShocks(**raw.get("ai_demand", {})),
            private_credit=PrivateCreditShocks(**raw.get("private_credit", {})),
            equity=EquityShocks(**raw.get("equity", {})),
            inflation=InflationShocks(**raw.get("inflation", {})),
            capex=CapexShocks(**raw.get("capex", {})),
        )


def load_scenario(name: str) -> Scenario:
    """Load a named scenario from scenarios.yaml."""
    scenarios = config.load_scenarios()
    if name not in scenarios:
        raise KeyError(f"Unknown scenario '{name}'. Available: {sorted(scenarios)}")
    return Scenario.from_dict(name, scenarios[name])


def load_all_scenarios() -> dict[str, Scenario]:
    return {
        name: Scenario.from_dict(name, raw)
        for name, raw in config.load_scenarios().items()
    }


def base_scenario() -> Scenario:
    """The zero-shock base scenario (falls back to an all-defaults Scenario)."""
    try:
        return load_scenario("base_case")
    except KeyError:
        return Scenario(name="base_case", label="Base case")
