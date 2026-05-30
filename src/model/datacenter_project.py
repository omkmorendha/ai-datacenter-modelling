"""Data-center project module: operating economics + coverage ratios.

Models a single data-center project (or a representative one) from capacity and
unit economics through to EBITDA, DSCR, ICR, FCF-after-maintenance-capex, and
the break-even analyses (utilization, power price, funding cost) that feed the
breakpoint panel.

Revenue is split contracted vs merchant; power cost scales with PUE; debt
service comes from the attached :class:`FinancingStack`. Scenario shocks flow
through power price, utilization, revenue/MW, and rate/spread (via the stack).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import config
from ..utils.validation import clamp, safe_div
from .financing_stack import AmortizationType, FinancingStack, build_representative_stack
from .scenarios import Scenario

HOURS_PER_YEAR = 8760


class DataCenterProject(BaseModel):
    name: str = "Representative AI data center"
    capacity_mw: float = Field(default=100.0, gt=0)
    capex_per_mw: float = 9_000_000.0
    utilization: float = Field(default=0.85, ge=0, le=1)
    contracted_revenue_per_mw_year: float = 1_650_000.0
    merchant_revenue_share: float = Field(default=0.15, ge=0, le=1)
    merchant_price_premium: float = 0.0   # merchant revenue uplift/discount vs contracted
    power_cost_per_mwh: float = 75.0
    pue: float = Field(default=1.25, ge=1.0)
    opex_ex_power_per_mw_year: float = 180_000.0
    maintenance_capex_pct_of_revenue: float = 0.05
    lease_term_years: int = 12
    tenant_credit_quality: str = "investment_grade"
    debt_to_capex_ratio: float = Field(default=0.65, ge=0, le=1)  # leverage on the build
    principal_amortization_rate: float = 0.0  # annual principal repaid / amortizing debt
    financing_stack: FinancingStack = Field(default_factory=FinancingStack)

    # ------------------------------------------------------------------ #
    # Capital
    # ------------------------------------------------------------------ #
    @property
    def total_capex(self) -> float:
        return self.capacity_mw * self.capex_per_mw

    @property
    def total_debt(self) -> float:
        if self.financing_stack.total_debt > 0:
            return self.financing_stack.total_debt
        return self.total_capex * self.debt_to_capex_ratio

    # ------------------------------------------------------------------ #
    # Operating economics (scenario-aware)
    # ------------------------------------------------------------------ #
    def _effective_utilization(self, scenario: Scenario | None) -> float:
        u = self.utilization
        if scenario is not None:
            u = u + scenario.ai_demand.utilization_delta
        return clamp(u, 0.0, 1.0)

    def _effective_revenue_per_mw(self, scenario: Scenario | None) -> float:
        rev = self.contracted_revenue_per_mw_year
        if scenario is not None:
            rev *= 1.0 + scenario.ai_demand.revenue_per_mw_pct
        return rev

    def _effective_power_price(self, scenario: Scenario | None) -> float:
        p = self.power_cost_per_mwh
        if scenario is not None:
            p *= 1.0 + scenario.power.power_price_pct
        return p

    def revenue(self, scenario: Scenario | None = None) -> float:
        u = self._effective_utilization(scenario)
        rev_per_mw = self._effective_revenue_per_mw(scenario)
        base = self.capacity_mw * rev_per_mw * u
        merchant = base * self.merchant_revenue_share * (1.0 + self.merchant_price_premium)
        contracted = base * (1.0 - self.merchant_revenue_share)
        return contracted + merchant

    def power_expense(self, scenario: Scenario | None = None) -> float:
        """Annual power cost: IT load * PUE * hours * utilization * $/MWh."""
        u = self._effective_utilization(scenario)
        price = self._effective_power_price(scenario)
        mwh = self.capacity_mw * self.pue * HOURS_PER_YEAR * u
        return mwh * price

    def opex_ex_power(self) -> float:
        return self.capacity_mw * self.opex_ex_power_per_mw_year

    def ebitda(self, scenario: Scenario | None = None) -> float:
        return self.revenue(scenario) - self.power_expense(scenario) - self.opex_ex_power()

    def ebitda_margin(self, scenario: Scenario | None = None) -> float:
        return safe_div(self.ebitda(scenario), self.revenue(scenario), default=0.0)

    # ------------------------------------------------------------------ #
    # Debt service & coverage
    # ------------------------------------------------------------------ #
    def interest_expense(self, scenario: Scenario | None = None) -> float:
        if self.financing_stack.total_debt > 0:
            return self.financing_stack.annual_interest_expense(scenario)
        wacd = self.financing_stack.weighted_average_cost_of_debt(scenario) or 0.07
        return self.total_debt * wacd

    def principal_due(self) -> float:
        """Annual principal amortization (excludes bullet maturities)."""
        amort_debt = sum(
            s.amount
            for s in self.financing_stack.sources
            if s.amortization_type == AmortizationType.AMORTIZING
        )
        return amort_debt * self.principal_amortization_rate

    def debt_service(self, scenario: Scenario | None = None) -> float:
        return self.interest_expense(scenario) + self.principal_due()

    def dscr(self, scenario: Scenario | None = None) -> float:
        """EBITDA / (interest + principal). >1 means cash covers debt service."""
        return safe_div(self.ebitda(scenario), self.debt_service(scenario), default=float("inf"))

    def icr(self, scenario: Scenario | None = None) -> float:
        """EBITDA / interest. Interest-coverage ratio."""
        return safe_div(self.ebitda(scenario), self.interest_expense(scenario), default=float("inf"))

    def maintenance_capex(self, scenario: Scenario | None = None) -> float:
        return self.revenue(scenario) * self.maintenance_capex_pct_of_revenue

    def fcf_after_capex(self, scenario: Scenario | None = None) -> float:
        """EBITDA - interest - maintenance capex (pre principal/tax)."""
        return (
            self.ebitda(scenario)
            - self.interest_expense(scenario)
            - self.maintenance_capex(scenario)
        )

    # ------------------------------------------------------------------ #
    # Break-even analysis (the breakpoint panel)
    # ------------------------------------------------------------------ #
    def break_even_utilization(self, scenario: Scenario | None = None) -> float:
        """Utilization at which EBITDA exactly covers debt service (DSCR = 1).

        revenue and power both scale linearly in u, so this is a linear solve:
          rev_slope*u - power_slope*u - fixed_opex = debt_service.
        """
        rev_per_mw = self._effective_revenue_per_mw(scenario)
        price = self._effective_power_price(scenario)
        rev_coef = self.capacity_mw * rev_per_mw * (
            (1 - self.merchant_revenue_share)
            + self.merchant_revenue_share * (1 + self.merchant_price_premium)
        )
        power_coef = self.capacity_mw * self.pue * HOURS_PER_YEAR * price
        ebitda_slope = rev_coef - power_coef
        target = self.debt_service(scenario) + self.opex_ex_power()
        return safe_div(target, ebitda_slope, default=float("nan"))

    def break_even_power_price(self, scenario: Scenario | None = None) -> float:
        """$/MWh at which EBITDA exactly covers debt service (DSCR = 1)."""
        u = self._effective_utilization(scenario)
        rev = self.revenue(scenario)
        mwh = self.capacity_mw * self.pue * HOURS_PER_YEAR * u
        target_power_budget = rev - self.opex_ex_power() - self.debt_service(scenario)
        return safe_div(target_power_budget, mwh, default=float("nan"))

    def break_even_funding_cost(self, scenario: Scenario | None = None) -> float:
        """Average funding cost (decimal) at which EBITDA exactly covers debt service,
        holding principal fixed."""
        ebitda = self.ebitda(scenario)
        interest_budget = ebitda - self.principal_due()
        return safe_div(interest_budget, self.total_debt, default=float("nan"))

    # ------------------------------------------------------------------ #
    # Bundled report
    # ------------------------------------------------------------------ #
    def metrics(self, scenario: Scenario | None = None) -> dict:
        thresholds = config.assumption_value("thresholds", {})
        dscr = self.dscr(scenario)
        icr = self.icr(scenario)
        margin = self.ebitda_margin(scenario)
        fcf = self.fcf_after_capex(scenario)
        return {
            "revenue": self.revenue(scenario),
            "power_expense": self.power_expense(scenario),
            "opex_ex_power": self.opex_ex_power(),
            "ebitda": self.ebitda(scenario),
            "ebitda_margin": margin,
            "interest_expense": self.interest_expense(scenario),
            "debt_service": self.debt_service(scenario),
            "dscr": dscr,
            "icr": icr,
            "fcf_after_capex": fcf,
            "break_even_utilization": self.break_even_utilization(scenario),
            "break_even_power_price": self.break_even_power_price(scenario),
            "break_even_funding_cost": self.break_even_funding_cost(scenario),
            "total_capex": self.total_capex,
            "total_debt": self.total_debt,
            "flags": {
                "dscr_danger": dscr < thresholds.get("dscr_min", 1.20),
                "icr_danger": icr < thresholds.get("icr_min", 1.50),
                "margin_danger": margin < thresholds.get("ebitda_margin_min", 0.45),
                "fcf_danger": fcf < thresholds.get("fcf_after_capex_min", 0.0),
            },
        }


# --------------------------------------------------------------------------- #
# Factory from base assumptions
# --------------------------------------------------------------------------- #
def build_representative_project(
    capacity_mw: float = 100.0,
    *,
    base_rates: dict[str, float] | None = None,
) -> DataCenterProject:
    """Build a representative project + attached financing stack from YAML."""
    av = config.assumption_value
    capex_per_mw = av("datacenter_capex_per_mw", 9_000_000)
    debt_to_capex = av("hyperscaler_capex_debt_funded_share", 0.25)
    # Project-level leverage is higher than the hyperscaler-balance-sheet share.
    project_leverage = max(0.65, float(debt_to_capex))
    total_debt = capacity_mw * capex_per_mw * project_leverage

    stack = build_representative_stack(
        total_debt,
        base_rates=base_rates,
        base_spread_bps=av("base_credit_spread_bps", 200),
    )

    return DataCenterProject(
        capacity_mw=capacity_mw,
        capex_per_mw=capex_per_mw,
        utilization=av("datacenter_utilization", 0.85),
        contracted_revenue_per_mw_year=av("datacenter_contracted_revenue_per_mw_year", 1_650_000),
        merchant_revenue_share=av("datacenter_merchant_revenue_share", 0.15),
        power_cost_per_mwh=av("datacenter_power_cost_per_mwh", 75),
        pue=av("datacenter_pue", 1.25),
        opex_ex_power_per_mw_year=av("datacenter_opex_ex_power_per_mw_year", 180_000),
        maintenance_capex_pct_of_revenue=av("datacenter_maintenance_capex_pct_of_revenue", 0.05),
        lease_term_years=av("datacenter_lease_term_years", 12),
        tenant_credit_quality=av("datacenter_tenant_credit_quality", "investment_grade"),
        debt_to_capex_ratio=project_leverage,
        principal_amortization_rate=0.04,
        financing_stack=stack,
    )
