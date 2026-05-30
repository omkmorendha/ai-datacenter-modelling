"""Contagion module: stylized network/exposure-matrix transmission across AI datacenter
financing ecosystem nodes.

THIS IS NOT A FORECAST. It is a stylized, manual-calibrated linear cascade model
intended to illustrate potential transmission channels in a stress scenario. The
exposure weights are illustrative approximations; private-credit estimates are
LOW-CONFIDENCE. No claim is made about real-world magnitudes.

Key distinctions honored:
  - MTM stress (mark-to-market losses) is distinguished from realized (default) losses.
  - Committed vs drawn: exposure weights proxy drawn/at-risk exposure.
  - Gross vs net floating: downstream nodes receive gross shock vectors.
  - Announced vs spent capex: capex_slowdown_estimate uses an assumption-based scale.

Spec nuances:
  - DEFAULT_NODES loaded from config.load_entity_universe()['contagion_nodes'] with
    a hardcoded fallback list if the YAML key is absent.
  - DEFAULT_EXPOSURES: sparse dict[to_node][from_node] = weight in [0,1].
  - ContagionModel is a PLAIN class (stateful, holds numpy matrix).
  - build_contagion_model() is the public factory.
  - build_initial_shock_from_scenario() is a staticmethod that duck-types: uses
    pc_book.mark_to_market_loss(scenario) and pc_book.expected_loss(scenario) if
    provided; otherwise scales rough $ from scenario shock magnitudes so the method
    always runs.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .. import config
from ..utils.logging import (
    get_logger,
    log_assumption_used,
    log_missing_data,
)
from ..utils.validation import clamp, safe_div
from .scenarios import Scenario

_logger = get_logger("ai_dc_model.contagion")

# ---------------------------------------------------------------------------
# Default node list (fallback; canonical source is entity_universe.yaml)
# ---------------------------------------------------------------------------
_FALLBACK_NODES: list[str] = [
    "hyperscalers",
    "datacenter_operators",
    "private_credit_funds",
    "bdcs",
    "insurers",
    "pensions",
    "banks",
    "corporate_bond_funds",
    "securitization_investors",
    "utilities",
    "chip_suppliers",
    "pe_funds_lps",
]


def _load_default_nodes() -> list[str]:
    """Load contagion node list from entity universe YAML; fallback to hardcoded list."""
    try:
        universe = config.load_entity_universe()
        nodes = universe.get("contagion_nodes")
        if nodes and isinstance(nodes, list):
            return [str(n) for n in nodes]
    except Exception as exc:
        _logger.warning("Could not load entity_universe.yaml: %s", exc)
    log_missing_data(_logger, what="contagion_nodes from entity_universe.yaml", fallback="hardcoded fallback list")
    return list(_FALLBACK_NODES)


DEFAULT_NODES: list[str] = _load_default_nodes()

# ---------------------------------------------------------------------------
# Default exposure matrix: dict[to_node][from_node] = weight (0..1)
# Sparse and reasonable. Weights represent directional dollar sensitivity:
# "how much does node[i]'s stress propagate to node[j]?"
# Private-credit exposure estimates are LOW-CONFIDENCE.
# ---------------------------------------------------------------------------
DEFAULT_EXPOSURES: dict[str, dict[str, float]] = {
    # Private credit funds are exposed to their own capital providers.
    "private_credit_funds": {
        "bdcs": 0.4,
        "insurers": 0.3,
        "pensions": 0.25,
        "banks": 0.15,
    },
    # BDCs get funding partly from the private credit market.
    "bdcs": {
        "private_credit_funds": 0.5,
    },
    # Data center operators are exposed to multiple credit channels + hyperscalers.
    "datacenter_operators": {
        "private_credit_funds": 0.3,
        "banks": 0.2,
        "securitization_investors": 0.2,
        "corporate_bond_funds": 0.15,
        "utilities": 0.15,
        "hyperscalers": 0.25,
    },
    # Banks lend to hyperscalers, operators, and provide warehouse lines to private credit.
    "banks": {
        "datacenter_operators": 0.25,
        "hyperscalers": 0.1,
        "private_credit_funds": 0.15,
    },
    # Insurers hold private credit and ABS/CMBS securitizations.
    "insurers": {
        "private_credit_funds": 0.2,
        "securitization_investors": 0.25,
    },
    # Pensions invest in private credit and PE fund LP stakes.
    "pensions": {
        "private_credit_funds": 0.2,
        "pe_funds_lps": 0.2,
    },
    # Securitization investors are exposed to the underlying datacenter operators.
    "securitization_investors": {
        "datacenter_operators": 0.3,
    },
    # Corporate bond funds hold hyperscaler and DC operator bonds.
    "corporate_bond_funds": {
        "hyperscalers": 0.2,
        "datacenter_operators": 0.2,
    },
    # Utilities have revenue exposure to data center power contracts.
    "utilities": {
        "datacenter_operators": 0.15,
    },
    # Chip suppliers are exposed to hyperscaler and operator capex cycles.
    "chip_suppliers": {
        "hyperscalers": 0.4,
        "datacenter_operators": 0.2,
    },
    # PE fund LPs are exposed to datacenter equity valuations and private credit.
    "pe_funds_lps": {
        "datacenter_operators": 0.3,
        "private_credit_funds": 0.2,
    },
    # Hyperscalers have some direct exposure to datacenter operators (JV / lease).
    "hyperscalers": {
        "datacenter_operators": 0.1,
    },
}

# Nodes that form the "credit intermediary" cluster for funding contraction.
_FUNDING_NODES: frozenset[str] = frozenset(
    ["banks", "private_credit_funds", "bdcs", "securitization_investors",
     "corporate_bond_funds", "insurers"]
)


# ---------------------------------------------------------------------------
# ContagionModel — PLAIN class (holds numpy matrix; pydantic would reject ndarray).
# ---------------------------------------------------------------------------

class ContagionModel:
    """Stylized linear cascade model for AI datacenter financing contagion.

    NOT a forecast. Exposure weights are manual/illustrative; private-credit
    estimates are LOW-CONFIDENCE. MTM stress != realized loss.

    Parameters
    ----------
    exposures:
        dict[to_node][from_node] = weight (0..1). Defaults to DEFAULT_EXPOSURES.
    nodes:
        Ordered list of node names. Defaults to DEFAULT_NODES.
    """

    def __init__(
        self,
        exposures: dict[str, dict[str, float]] | None = None,
        nodes: list[str] | None = None,
    ) -> None:
        self.nodes: list[str] = list(nodes) if nodes is not None else list(DEFAULT_NODES)
        self._exposures: dict[str, dict[str, float]] = exposures if exposures is not None else DEFAULT_EXPOSURES
        n = len(self.nodes)
        # Build M[i, j] = weight from node j to node i (i.e. how much j's shock
        # propagates into i). Column j drives, row i receives.
        self._M: np.ndarray = np.zeros((n, n), dtype=float)
        for i, to_node in enumerate(self.nodes):
            from_weights = self._exposures.get(to_node, {})
            for j, from_node in enumerate(self.nodes):
                self._M[i, j] = from_weights.get(from_node, 0.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _shock_vector(self, initial_shock: dict[str, float]) -> np.ndarray:
        """Convert node->$ shock dict to aligned numpy vector (missing nodes = 0)."""
        vec = np.zeros(len(self.nodes), dtype=float)
        for node, val in initial_shock.items():
            if node in self.nodes:
                idx = self.nodes.index(node)
                vec[idx] = float(val)
            else:
                _logger.debug("Shock node '%s' not in node list; ignored.", node)
        return vec

    def _vec_to_dict(self, vec: np.ndarray) -> dict[str, float]:
        return {node: float(vec[i]) for i, node in enumerate(self.nodes)}

    # ------------------------------------------------------------------
    # Core propagation methods
    # ------------------------------------------------------------------

    def first_order_loss(self, initial_shock: dict[str, float]) -> dict[str, float]:
        """Compute first-order (one-hop) propagated loss via M @ shock_vector.

        Each node receives the sum of (exposure_weight * from_node_shock) over
        all predecessor nodes. This is a single matrix-vector multiply.

        Returns
        -------
        dict[node -> $ first-order propagated loss]
        """
        x = self._shock_vector(initial_shock)
        result = self._M @ x
        return self._vec_to_dict(result)

    def propagate(
        self,
        initial_shock: dict[str, float],
        *,
        rounds: int = 3,
        decay: float = 0.6,
    ) -> dict[str, float]:
        """Stylized linear cascade: cumulative loss over multiple rounds.

        Each round r: new_shock = decay * (M @ prev_shock). Cumulative total
        accumulates across all rounds plus the initial shock. The decay factor
        represents damping / partial loss absorption at each hop.

        This is a STYLIZED model — not a calibrated econometric cascade. It
        captures second- and third-order feedback in qualitative terms only.

        Parameters
        ----------
        initial_shock:
            Node -> $ initial shock (e.g. MTM loss at private credit funds).
        rounds:
            Number of propagation rounds (default 3). More rounds = longer tail.
        decay:
            Multiplier on each successive round (0..1). 0.6 implies 40% damping
            per hop.

        Returns
        -------
        dict[node -> cumulative $ including initial shock and all rounds]
        """
        x = self._shock_vector(initial_shock)
        total = x.copy()
        for _ in range(rounds):
            x = decay * (self._M @ x)
            total += x
        return self._vec_to_dict(total)

    def second_order_mtm_loss(
        self,
        initial_shock: dict[str, float],
        **kw: Any,
    ) -> dict[str, float]:
        """Cumulative propagated loss MINUS initial and first-order components.

        Isolates the second-order-and-beyond (indirect) channel only.
        'mtm_loss' label is shorthand; actual losses here are stylized cascade
        estimates — not mark-to-market in the strict accounting sense.

        Returns
        -------
        dict[node -> $ indirect (second-order+) loss]
        """
        rounds = kw.pop("rounds", 3)
        decay = kw.pop("decay", 0.6)
        x0 = self._shock_vector(initial_shock)
        first = self._M @ x0
        total_vec = np.zeros(len(self.nodes), dtype=float)
        x = x0.copy()
        total_vec += x
        for _ in range(rounds):
            x = decay * (self._M @ x)
            total_vec += x
        # Subtract initial and first-order to isolate the second-order-and-beyond tail.
        indirect = total_vec - x0 - first
        return self._vec_to_dict(indirect)

    # ------------------------------------------------------------------
    # Higher-level stress indicators
    # ------------------------------------------------------------------

    def funding_contraction_estimate(
        self,
        initial_shock: dict[str, float],
        *,
        multiplier: float = 5.0,
    ) -> float:
        """Estimate aggregate funding contraction implied by propagated losses.

        Sums total propagated losses at credit-intermediary nodes (banks,
        private_credit_funds, bdcs, securitization_investors, corporate_bond_funds,
        insurers) and applies a leverage multiplier to approximate the balance-sheet
        contraction effect.

        The multiplier (~5x) is a stylized leverage proxy: a $1 loss at a bank
        with 10x leverage could contract $10 of lending capacity, but not all
        capacity is allocated to AI/DC. 5x is a conservative midpoint estimate
        (LOW-CONFIDENCE).

        Returns
        -------
        Estimated dollar funding contraction (USD).
        """
        total = self.propagate(initial_shock)
        funding_loss = sum(
            total.get(node, 0.0) for node in _FUNDING_NODES
        )
        return funding_loss * multiplier

    def capex_slowdown_estimate(self, initial_shock: dict[str, float]) -> float:
        """Estimate fractional AI datacenter capex slowdown (0..1) from shock.

        Combines:
          - Funding contraction / private-credit-outstanding scale (credit channel)
          - Direct losses at hyperscalers + datacenter_operators / scale (equity/liquidity channel)

        Scale is config.assumption_value('private_credit_ai_datacenter_outstanding');
        if missing, falls back to $250B.

        Returns
        -------
        Capex slowdown fraction in [0, 1] (0 = no slowdown, 1 = full halt).
        This is a stylized ordinal indicator, NOT a calibrated econometric estimate.
        """
        scale = config.assumption_value("private_credit_ai_datacenter_outstanding", 250e9)
        if scale is None or scale <= 0:
            scale = 250e9
        log_assumption_used(
            _logger,
            key="private_credit_ai_datacenter_outstanding",
            value=scale,
            confidence="low",
        )
        funding_contraction = self.funding_contraction_estimate(initial_shock)
        total = self.propagate(initial_shock)
        direct_losses = total.get("hyperscalers", 0.0) + total.get("datacenter_operators", 0.0)
        raw = safe_div(funding_contraction, scale, default=0.0) + safe_div(direct_losses, scale, default=0.0)
        return clamp(raw, 0.0, 1.0)

    def private_market_liquidity_stress_score(
        self, initial_shock: dict[str, float]
    ) -> float:
        """Composite private-market liquidity stress score in [0, 1].

        Aggregates propagated losses at illiquid/private-market nodes
        (private_credit_funds, bdcs, pe_funds_lps, pensions, insurers) relative
        to the private-credit-outstanding scale. Higher = more stressed.

        LOW-CONFIDENCE. Private market pricing is opaque and lagged; this score
        reflects potential stress, not observable prices.

        Returns
        -------
        Stress score in [0, 1].
        """
        scale = config.assumption_value("private_credit_ai_datacenter_outstanding", 250e9)
        if scale is None or scale <= 0:
            scale = 250e9
        private_nodes = frozenset(
            ["private_credit_funds", "bdcs", "pe_funds_lps", "pensions", "insurers"]
        )
        total = self.propagate(initial_shock)
        agg = sum(total.get(n, 0.0) for n in private_nodes)
        return clamp(safe_div(agg, scale, default=0.0), 0.0, 1.0)

    def systemic_risk_heatmap(
        self, initial_shock: dict[str, float]
    ) -> pd.DataFrame:
        """Build a per-node risk heatmap DataFrame.

        Columns:
          - first_order: $ first-order propagated loss
          - total_loss: $ cumulative propagated loss (including initial shock)
          - share_of_total: node's share of aggregate total_loss

        Index: node names.

        Returns
        -------
        pandas.DataFrame indexed by node, three columns.
        """
        fo = self.first_order_loss(initial_shock)
        tot = self.propagate(initial_shock)
        grand_total = sum(tot.values())
        rows: list[dict[str, float]] = []
        for node in self.nodes:
            t = tot.get(node, 0.0)
            rows.append(
                {
                    "first_order": fo.get(node, 0.0),
                    "total_loss": t,
                    "share_of_total": safe_div(t, grand_total, default=0.0),
                }
            )
        return pd.DataFrame(rows, index=self.nodes)

    # ------------------------------------------------------------------
    # Shock builder
    # ------------------------------------------------------------------

    @staticmethod
    def build_initial_shock_from_scenario(
        scenario: Scenario,
        *,
        pc_book: Any | None = None,
        project: Any | None = None,
        hyperscaler: Any | None = None,
    ) -> dict[str, float]:
        """Build an initial shock dict from a Scenario and optional model objects.

        Duck-typed: if pc_book is provided and has mark_to_market_loss / expected_loss
        methods, uses them for private_credit_funds / bdcs / insurers / pensions /
        securitization_investors. If project is provided and has mark_to_market_loss /
        ebitda loss, uses it for datacenter_operators. Otherwise uses scenario shock
        magnitudes scaled to config's private-credit outstanding assumption.

        The pe_funds_lps shock uses scenario.equity.valuation_haircut * a notional
        equity pool (10% of private-credit scale by convention).

        All estimates are ROUGH / LOW-CONFIDENCE placeholders.

        Parameters
        ----------
        scenario: Scenario
            The stress scenario to extract shocks from.
        pc_book: optional
            Private credit book object; must expose mark_to_market_loss(scenario)
            and optionally expected_loss(scenario) (duck-typed).
        project: optional
            DataCenterProject (or similar) with mark_to_market_loss(scenario)
            and ebitda(scenario) methods (duck-typed).
        hyperscaler: optional
            Hyperscaler-level object with a revenue/ebitda loss method (duck-typed).

        Returns
        -------
        dict[node -> $ shock] — always non-empty (falls back to assumption-scaled estimates).
        """
        scale = config.assumption_value("private_credit_ai_datacenter_outstanding", 250e9)
        if scale is None or scale <= 0:
            scale = 250e9
        log_assumption_used(
            _logger,
            key="private_credit_ai_datacenter_outstanding",
            value=scale,
            confidence="low",
        )

        shock: dict[str, float] = {}

        # ---- private credit / structured products ----
        if pc_book is not None:
            # duck-typed: try MTM loss first, then expected loss
            try:
                mtm = pc_book.mark_to_market_loss(scenario)
                shock["private_credit_funds"] = float(mtm)
            except Exception:
                pass
            try:
                el = pc_book.expected_loss(scenario)
                # Distribute realized losses to BDC/insurer/pension/secur channels
                shock["bdcs"] = shock.get("bdcs", 0.0) + float(el) * 0.3
                shock["insurers"] = shock.get("insurers", 0.0) + float(el) * 0.2
                shock["pensions"] = shock.get("pensions", 0.0) + float(el) * 0.15
                shock["securitization_investors"] = (
                    shock.get("securitization_investors", 0.0) + float(el) * 0.15
                )
            except Exception:
                pass

        # ---- datacenter operator losses ----
        if project is not None:
            try:
                dc_mtm = project.mark_to_market_loss(scenario)
                shock["datacenter_operators"] = float(dc_mtm)
            except Exception:
                pass
            if "datacenter_operators" not in shock:
                try:
                    # proxy: EBITDA compression as a rough stress dollar
                    from .scenarios import base_scenario as _base_scenario
                    base = _base_scenario()
                    ebitda_base = project.ebitda(base)
                    ebitda_stress = project.ebitda(scenario)
                    shock["datacenter_operators"] = float(max(0.0, ebitda_base - ebitda_stress))
                except Exception:
                    pass

        # ---- hyperscaler losses ----
        if hyperscaler is not None:
            try:
                hs_loss = hyperscaler.mark_to_market_loss(scenario)
                shock["hyperscalers"] = float(hs_loss)
            except Exception:
                pass

        # ---- fallback: scale from scenario shock magnitudes ----
        # This path always runs for nodes not already populated.
        nav_haircut = float(scenario.private_credit.nav_haircut)
        credit_bps = float(scenario.credit.credit_spread_bps)
        rate_bps = float(scenario.rates.sofr_bps)
        equity_haircut = float(scenario.equity.valuation_haircut)
        ai_demand_delta = float(scenario.ai_demand.utilization_delta)

        # Scale: private credit outstanding * NAV haircut -> fund-level stress
        if "private_credit_funds" not in shock:
            shock["private_credit_funds"] = scale * abs(nav_haircut)

        # BDCs similarly affected (partial overlap, so 40% of pc scale)
        if "bdcs" not in shock:
            shock["bdcs"] = scale * 0.4 * abs(nav_haircut)

        # Securitization investors: credit spread shock approximation
        # $100B notional securitized AI/DC ABS * spread/10000 * rough duration 5y
        secur_notional = scale * 0.3
        mtm_per_unit = (abs(credit_bps) / 10_000) * 5.0  # duration proxy
        if "securitization_investors" not in shock:
            shock["securitization_investors"] = secur_notional * mtm_per_unit

        # Banks: rate + credit spread exposure on loan books
        bank_book = scale * 0.25  # rough bank share of AI/DC lending
        bank_shock = bank_book * (abs(rate_bps) + abs(credit_bps)) / 10_000 * 3.0
        if "banks" not in shock:
            shock["banks"] = bank_shock

        # Insurers: credit spread / ABS losses
        insurer_notional = scale * 0.2
        if "insurers" not in shock:
            shock["insurers"] = insurer_notional * mtm_per_unit

        # Pensions: private credit NAV
        pension_notional = scale * 0.15
        if "pensions" not in shock:
            shock["pensions"] = pension_notional * abs(nav_haircut)

        # Corporate bond funds: rate + spread on public bonds
        cbf_notional = scale * 0.15
        cbf_shock = cbf_notional * (abs(rate_bps) + abs(credit_bps)) / 10_000 * 5.0
        if "corporate_bond_funds" not in shock:
            shock["corporate_bond_funds"] = cbf_shock

        # PE fund LPs: equity valuation haircut on notional equity pool
        # Convention: equity pool = 10% of private-credit-outstanding scale
        equity_pool = scale * 0.10
        if "pe_funds_lps" not in shock:
            shock["pe_funds_lps"] = equity_pool * abs(equity_haircut)

        # Hyperscalers: AI demand shock translates to revenue/capex impact
        # Revenue proxy: hyperscaler AI revenue ~$200B, utilization delta * revenue
        hs_revenue_proxy = 200e9
        if "hyperscalers" not in shock:
            shock["hyperscalers"] = hs_revenue_proxy * abs(ai_demand_delta)

        # Datacenter operators: demand + credit
        dc_notional = scale * 0.5
        if "datacenter_operators" not in shock:
            shock["datacenter_operators"] = dc_notional * (
                abs(nav_haircut) + abs(ai_demand_delta) * 0.5
            )

        # Utilities: power contract disruption (small relative to others)
        if "utilities" not in shock:
            shock["utilities"] = scale * 0.05 * abs(ai_demand_delta)

        # Chip suppliers: hyperscaler capex cuts
        capex_cut = float(scenario.capex.capex_cut_pct)
        chip_revenue_proxy = 150e9  # rough AI chip annual revenue
        if "chip_suppliers" not in shock:
            shock["chip_suppliers"] = chip_revenue_proxy * abs(capex_cut)

        return shock


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_contagion_model(
    exposures: dict[str, dict[str, float]] | None = None,
    nodes: list[str] | None = None,
) -> ContagionModel:
    """Factory: build a ContagionModel with optional custom exposures/nodes.

    Defaults to DEFAULT_EXPOSURES and the node list from entity_universe.yaml.
    """
    return ContagionModel(exposures=exposures, nodes=nodes)
