"""Streamlit dashboard for the AI data-center macro stress model.

Run with:  uv run streamlit run src/app/streamlit_app.py

Panels (per spec):
  - scenario selector
  - editable assumptions table
  - financing-stack view
  - Treasury curve / SOFR view
  - DSCR & ICR outputs
  - refinancing wall chart
  - sensitivity table
  - contagion heatmap
  - investor loss allocation chart
  - breakpoint panel

Every cross-module import is defensive: if a module or remote data source is
unavailable, the panel shows a clear notice and the rest of the app still works.
This is a STRESS-TESTING tool, not a forecast — the sidebar repeats that.
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

# Allow `streamlit run src/app/streamlit_app.py` to find the `src` package.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from src import config  # noqa: E402
from src.model.datacenter_project import build_representative_project  # noqa: E402
from src.model.scenarios import load_all_scenarios  # noqa: E402
from src.model.stress_metrics import (  # noqa: E402
    compute_breakpoints,
    refinancing_gap,
    run_scenario,
    sensitivity_table,
)

st.set_page_config(
    page_title="AI Data-Center Macro Stress Model",
    page_icon="🏗️",
    layout="wide",
)


# --------------------------------------------------------------------------- #
# Optional modules (defensive)
# --------------------------------------------------------------------------- #
def _opt_import(path: str, attr: str):
    try:
        mod = __import__(path, fromlist=[attr])
        return getattr(mod, attr)
    except Exception:
        return None


build_treasury_module = _opt_import("src.model.treasury_module", "build_treasury_module")
build_private_credit_book = _opt_import("src.model.private_credit_module", "build_private_credit_book")
build_hyperscaler_module = _opt_import("src.model.hyperscaler_module", "build_hyperscaler_module")
build_power_module = _opt_import("src.model.power_module", "build_power_module")
build_contagion_model = _opt_import("src.model.contagion_module", "build_contagion_model")
list_assumptions = _opt_import("src.data_sources.manual_inputs", "list_assumptions")
FredClient = _opt_import("src.data_sources.fred", "FredClient")


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def get_scenarios() -> dict:
    return {name: sc.model_dump() for name, sc in load_all_scenarios().items()}


@st.cache_data(show_spinner=False)
def get_fred_levels() -> dict:
    """Best-effort current Treasury/SOFR levels; falls back to assumptions."""
    if FredClient is None or config.is_offline():
        return {}
    try:
        client = FredClient()
        return client.get_latest_levels() or {}
    except Exception:
        return {}


def fmt_money(x: float) -> str:
    if x is None:
        return "n/a"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


# --------------------------------------------------------------------------- #
# Sidebar — scenario selector + global controls
# --------------------------------------------------------------------------- #
st.sidebar.title("🏗️ AI DC Stress Model")
st.sidebar.caption(
    "A scenario-based **stress-testing tool**, not a forecast. Many inputs are "
    "low-confidence assumptions, not facts."
)

all_scenarios = load_all_scenarios()
scenario_names = list(all_scenarios)
selected_name = st.sidebar.selectbox(
    "Scenario",
    scenario_names,
    index=0,
    format_func=lambda n: all_scenarios[n].label or n,
)
base_scenario_obj = all_scenarios[selected_name]

capacity_mw = st.sidebar.number_input(
    "Representative project size (MW)", min_value=1.0, max_value=2000.0, value=100.0, step=10.0
)

st.sidebar.markdown("### Manual shock overrides")
st.sidebar.caption("Tweak the selected scenario's shocks live.")
ov_sofr = st.sidebar.slider("SOFR shock (bps)", -50, 400, int(base_scenario_obj.rates.sofr_bps), 25)
ov_10y = st.sidebar.slider("10Y shock (bps)", -50, 400, int(base_scenario_obj.rates.dgs10_bps), 25)
ov_30y = st.sidebar.slider("30Y shock (bps)", -50, 400, int(base_scenario_obj.rates.dgs30_bps), 25)
ov_spread = st.sidebar.slider("Credit spread shock (bps)", 0, 500, int(base_scenario_obj.credit.credit_spread_bps), 25)
ov_power = st.sidebar.slider("Power price shock (%)", -20, 100, int(base_scenario_obj.power.power_price_pct * 100), 5)
ov_util = st.sidebar.slider("Utilization shock (pp)", -40, 10, int(base_scenario_obj.ai_demand.utilization_delta * 100), 5)

# Build the effective scenario from selection + overrides
scenario = deepcopy(base_scenario_obj)
scenario.rates.sofr_bps = ov_sofr
scenario.rates.dgs10_bps = ov_10y
scenario.rates.dgs30_bps = ov_30y
scenario.credit.credit_spread_bps = ov_spread
scenario.power.power_price_pct = ov_power / 100.0
scenario.ai_demand.utilization_delta = ov_util / 100.0


# --------------------------------------------------------------------------- #
# Build model objects
# --------------------------------------------------------------------------- #
fred_levels = get_fred_levels()
base_rates = None
if fred_levels:
    base_rates = {
        "sofr": fred_levels.get("SOFR"),
        "2y": fred_levels.get("DGS2"),
        "10y": fred_levels.get("DGS10"),
        "30y": fred_levels.get("DGS30"),
    }
    base_rates = {k: v for k, v in base_rates.items() if v is not None} or None

project = build_representative_project(capacity_mw=capacity_mw, base_rates=base_rates)

mods = {}
if build_treasury_module:
    try:
        mods["treasury"] = build_treasury_module(base_rates=base_rates)
    except Exception:
        pass
if build_private_credit_book:
    try:
        mods["private_credit"] = build_private_credit_book()
    except Exception:
        pass
if build_hyperscaler_module:
    try:
        mods["hyperscaler"] = build_hyperscaler_module()
    except Exception:
        pass
if build_power_module:
    try:
        mods["power"] = build_power_module()
    except Exception:
        pass
if build_contagion_model:
    try:
        mods["contagion"] = build_contagion_model()
    except Exception:
        pass

result = run_scenario(scenario, project=project, **mods)
pm = result["project"]
fs = result["financing_stack"]


# --------------------------------------------------------------------------- #
# Header + headline metrics
# --------------------------------------------------------------------------- #
st.title("AI Data-Center Macro Stress Model")
st.caption(f"**{scenario.label}** — {scenario.description}")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("DSCR", f"{pm['dscr']:.2f}", help="EBITDA / (interest + principal)")
c2.metric("ICR", f"{pm['icr']:.2f}", help="EBITDA / interest")
c3.metric("EBITDA margin", f"{pm['ebitda_margin'] * 100:.1f}%")
c4.metric("WACD", f"{fs['wacd'] * 100:.2f}%", help="Weighted average cost of debt")
c5.metric("FCF after capex", fmt_money(pm["fcf_after_capex"]))

flags = pm["flags"]
danger = [k.replace("_danger", "").upper() for k, v in flags.items() if v]
if danger:
    st.error("⚠️ Danger flags triggered: " + ", ".join(danger))
else:
    st.success("✅ No danger thresholds breached under this scenario.")


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
tabs = st.tabs([
    "Project & Coverage",
    "Financing stack",
    "Treasury / SOFR",
    "Refinancing wall",
    "Sensitivity",
    "Breakpoints",
    "Private credit & contagion",
    "Assumptions",
])

# ---- Tab 1: Project & coverage -------------------------------------------- #
with tabs[0]:
    st.subheader("Project economics")
    left, right = st.columns(2)
    with left:
        rows = {
            "Total capex": fmt_money(pm["total_capex"]),
            "Total debt": fmt_money(pm["total_debt"]),
            "Revenue": fmt_money(pm["revenue"]),
            "Power expense": fmt_money(pm["power_expense"]),
            "Opex ex-power": fmt_money(pm["opex_ex_power"]),
            "EBITDA": fmt_money(pm["ebitda"]),
            "Interest expense": fmt_money(pm["interest_expense"]),
            "Debt service": fmt_money(pm["debt_service"]),
            "FCF after maint. capex": fmt_money(pm["fcf_after_capex"]),
        }
        st.table(pd.DataFrame(rows.items(), columns=["Metric", "Value"]))
    with right:
        # DSCR/ICR across all scenarios for context
        comp = []
        for name, sc in all_scenarios.items():
            m = project.metrics(sc)
            comp.append({"scenario": name, "DSCR": m["dscr"], "ICR": m["icr"],
                         "margin": m["ebitda_margin"]})
        cdf = pd.DataFrame(comp)
        fig = go.Figure()
        fig.add_bar(x=cdf["scenario"], y=cdf["DSCR"], name="DSCR")
        fig.add_bar(x=cdf["scenario"], y=cdf["ICR"], name="ICR")
        fig.add_hline(y=config.assumption_value("thresholds", {}).get("dscr_min", 1.2),
                      line_dash="dash", line_color="red", annotation_text="DSCR min")
        fig.update_layout(barmode="group", title="DSCR / ICR by scenario",
                          xaxis_tickangle=-30, height=420, margin=dict(t=40))
        st.plotly_chart(fig, use_container_width=True)

# ---- Tab 2: Financing stack ----------------------------------------------- #
with tabs[1]:
    st.subheader("Mixed financing stack")
    st.caption("Explicitly NOT 'all floating-rate private credit' — a mixed capital stack.")
    src_rows = []
    for s in project.financing_stack.sources:
        src_rows.append({
            "Source": s.name,
            "Amount": s.amount,
            "Share": s.amount / max(project.financing_stack.total_debt, 1),
            "Type": s.fixed_or_floating.value,
            "Ref": s.base_rate_reference,
            "Spread (bps)": s.credit_spread_bps,
            "All-in rate": s.all_in_rate(scenario),
            "Maturity": s.maturity_year,
            "Hedge": s.hedge_ratio,
            "Swap→fixed": s.swapped_to_fixed_ratio,
            "Lender": s.lender_type.value,
        })
    sdf = pd.DataFrame(src_rows)
    st.dataframe(
        sdf.style.format({
            "Amount": "${:,.0f}", "Share": "{:.1%}", "All-in rate": "{:.2%}",
            "Hedge": "{:.0%}", "Swap→fixed": "{:.0%}",
        }),
        use_container_width=True,
    )
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Floating share (gross)", f"{fs['floating_share_gross'] * 100:.1f}%")
    cc2.metric("Floating share (net of hedges)", f"{fs['floating_share_net'] * 100:.1f}%")
    cc3.metric("MTM loss (scenario)", fmt_money(fs["mtm_loss"]))

    colA, colB = st.columns(2)
    with colA:
        fig = px.pie(sdf, names="Source", values="Amount", title="Capital stack composition")
        fig.update_layout(height=400, margin=dict(t=40))
        st.plotly_chart(fig, use_container_width=True)
    with colB:
        lender_df = pd.DataFrame(
            fs["exposure_by_lender_type"].items(), columns=["Lender type", "Exposure"]
        )
        fig = px.bar(lender_df, x="Lender type", y="Exposure", title="Exposure by lender type")
        fig.update_layout(height=400, margin=dict(t=40), xaxis_tickangle=-30)
        st.plotly_chart(fig, use_container_width=True)

# ---- Tab 3: Treasury / SOFR ----------------------------------------------- #
with tabs[2]:
    st.subheader("Treasury curve / SOFR")
    if fred_levels:
        st.caption("Live levels from FRED.")
    else:
        st.info("No FRED data (offline or no API key) — using YAML/default levels.")
    base = base_rates or {"sofr": 0.0433, "2y": 0.043, "10y": 0.045, "30y": 0.047}
    curve_rows = []
    for label, key, shock_attr in [
        ("SOFR", "sofr", "sofr_bps"), ("2Y", "2y", "dgs2_bps"),
        ("10Y", "10y", "dgs10_bps"), ("30Y", "30y", "dgs30_bps"),
    ]:
        lvl = base.get(key, 0.0)
        shock = getattr(scenario.rates, shock_attr) / 10000.0
        curve_rows.append({"Tenor": label, "Base": lvl, "Shocked": lvl + shock})
    curve = pd.DataFrame(curve_rows)
    fig = go.Figure()
    fig.add_scatter(x=curve["Tenor"], y=curve["Base"] * 100, mode="lines+markers", name="Base")
    fig.add_scatter(x=curve["Tenor"], y=curve["Shocked"] * 100, mode="lines+markers", name="Shocked")
    fig.update_layout(title="Yield curve (base vs scenario)", yaxis_title="%", height=420)
    st.plotly_chart(fig, use_container_width=True)

    if "treasury" in mods:
        try:
            tsum = mods["treasury"].summary(scenario)
            tc1, tc2, tc3 = st.columns(3)
            tc1.metric("Treasury Stress Index", f"{tsum.get('treasury_stress_index', 0):.2f}")
            tc2.metric("Securitization openness", f"{tsum.get('securitization_openness_score', 0):.2f}")
            tc3.metric("Refi cost shock", f"{tsum.get('refinancing_cost_shock', 0) * 100:.2f}%")
        except Exception as exc:
            st.warning(f"Treasury module summary unavailable: {exc}")
    else:
        st.info("Treasury module not available — index/openness panel hidden.")

# ---- Tab 4: Refinancing wall ---------------------------------------------- #
with tabs[3]:
    st.subheader("Refinancing wall")
    gap = refinancing_gap(project, scenario=scenario)
    gdf = pd.DataFrame([
        {"Year": y, "Maturing principal": g["maturing_principal"],
         "Principal at risk": g["principal_at_risk"], "Refi prob": g["refi_probability"]}
        for y, g in gap.items()
    ])
    fig = go.Figure()
    fig.add_bar(x=gdf["Year"], y=gdf["Maturing principal"], name="Maturing principal")
    fig.add_bar(x=gdf["Year"], y=gdf["Principal at risk"], name="Principal at risk",
                marker_color="crimson")
    fig.update_layout(barmode="overlay", title="Maturity wall vs principal at risk",
                      yaxis_title="USD", height=420)
    st.plotly_chart(fig, use_container_width=True)
    refi_prob = gdf["Refi prob"].iloc[0] if len(gdf) else 0
    st.caption(f"Scenario-adjusted refinancing probability: {refi_prob * 100:.0f}%  "
               f"(base 90% ± scenario delta). 'At risk' = maturing × (1 − refi prob).")

# ---- Tab 5: Sensitivity --------------------------------------------------- #
with tabs[4]:
    st.subheader("Sensitivity table")
    axis = st.selectbox("Shock axis", [
        "sofr_bps", "dgs10_bps", "credit_spread_bps",
        "power_price_pct", "utilization_delta", "revenue_per_mw_pct",
    ])
    rows = sensitivity_table(project, axis=axis)
    rdf = pd.DataFrame(rows)
    st.dataframe(
        rdf.style.format({
            "dscr": "{:.2f}", "icr": "{:.2f}", "ebitda_margin": "{:.1%}",
            "fcf_after_capex": "${:,.0f}", "wacd": "{:.2%}",
        }),
        use_container_width=True,
    )
    fig = px.line(rdf, x=axis, y=["dscr", "icr"], markers=True,
                  title=f"DSCR / ICR vs {axis}")
    fig.add_hline(y=1.0, line_dash="dot", line_color="grey")
    fig.update_layout(height=400)
    st.plotly_chart(fig, use_container_width=True)

# ---- Tab 6: Breakpoints --------------------------------------------------- #
with tabs[5]:
    st.subheader("Breakpoint panel")
    st.caption("One-axis sensitivity from the base case: how much room before each metric breaks.")
    bp = compute_breakpoints(project)
    panel = [
        ("Max SOFR before DSCR breach", bp["max_sofr_shock_bps_before_dscr_breach"], "bps"),
        ("Max 10Y before refi uneconomic", bp["max_10y_shock_bps_before_refi_uneconomic"], "bps"),
        ("Max power price before margin breach", bp["max_power_price_pct_before_margin_breach"], "pct"),
        ("Min utilization before covenant breach", bp["min_utilization_before_covenant_breach"], "abs"),
        ("Max credit spread before FCF<0", bp["max_credit_spread_bps_before_fcf_negative"], "bps"),
    ]
    cols = st.columns(len(panel))
    for col, (label, val, kind) in zip(cols, panel, strict=False):
        if val is None:
            disp = "no breach"
        elif kind == "bps":
            disp = f"+{val:.0f} bps"
        elif kind == "pct":
            disp = f"+{val * 100:.0f}%"
        else:
            disp = f"{val * 100:.1f}%"
        col.metric(label, disp)
    st.caption(f"Thresholds used: {bp['thresholds_used']}")

# ---- Tab 7: Private credit & contagion ------------------------------------ #
with tabs[6]:
    st.subheader("Private credit & contagion")
    st.warning(
        "Private-credit, SPV, and AI-capex exposures are **opaque** — these are "
        "LOW-CONFIDENCE assumptions. Use the sensitivity, not the point estimates."
    )
    if "private_credit" in result:
        pc = result["private_credit"]
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Expected loss (realized)", fmt_money(pc.get("expected_loss")))
        p2.metric("MTM loss", fmt_money(pc.get("mark_to_market_loss")))
        p3.metric("Liquidity stress", f"{pc.get('liquidity_stress_score', 0):.2f}")
        p4.metric("Redemption/gating stress", f"{pc.get('redemption_gating_stress_proxy', 0):.2f}")
        alloc = pc.get("investor_loss_allocation")
        if alloc:
            adf = pd.DataFrame(alloc.items(), columns=["Investor", "Loss"])
            fig = px.bar(adf.sort_values("Loss", ascending=False), x="Investor", y="Loss",
                         title="Investor loss allocation")
            fig.update_layout(height=400, xaxis_tickangle=-30)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Private-credit module not available.")

    if "contagion" in result:
        cg = result["contagion"]
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Funding contraction", fmt_money(cg.get("funding_contraction")))
        cc2.metric("Capex slowdown", f"{cg.get('capex_slowdown', 0) * 100:.1f}%")
        cc3.metric("Liquidity stress", f"{cg.get('liquidity_stress', 0):.2f}")
        # Heatmap of propagated losses
        if "contagion" in mods:
            try:
                shock = mods["contagion"].build_initial_shock_from_scenario(
                    scenario, pc_book=mods.get("private_credit"), project=project,
                    hyperscaler=mods.get("hyperscaler"),
                )
                heat = mods["contagion"].systemic_risk_heatmap(shock)
                fig = px.imshow(
                    heat[["first_order", "total_loss"]].T,
                    labels=dict(x="Node", y="Loss type", color="USD"),
                    aspect="auto", title="Systemic risk heatmap (loss by node)",
                    color_continuous_scale="Reds",
                )
                fig.update_layout(height=320, xaxis_tickangle=-40)
                st.plotly_chart(fig, use_container_width=True)
            except Exception as exc:
                st.warning(f"Contagion heatmap unavailable: {exc}")
    else:
        st.info("Contagion module not available.")

# ---- Tab 8: Assumptions --------------------------------------------------- #
with tabs[7]:
    st.subheader("Assumptions (editable view) & provenance")
    st.caption(
        "Every manually-estimated value carries value/unit/confidence/source_note/"
        "rationale. Many are low-confidence scenario inputs, not facts. Editing here "
        "is a live override for this session (it does not write the YAML)."
    )
    if list_assumptions:
        try:
            adf = list_assumptions()
            conf_filter = st.multiselect(
                "Filter by confidence", ["low", "medium", "high", "unknown"],
                default=["low", "medium", "high", "unknown"],
            )
            if "confidence" in adf.columns:
                adf = adf[adf["confidence"].isin(conf_filter)]
            st.dataframe(adf, use_container_width=True, height=500)
        except Exception as exc:
            st.warning(f"manual_inputs unavailable: {exc}")
    else:
        # fallback: raw YAML blocks
        ba = config.load_base_assumptions()
        rows = []
        for k, v in ba.items():
            if isinstance(v, dict):
                rows.append({"key": k, "value": str(v.get("value")),
                             "confidence": v.get("confidence", ""),
                             "source_note": v.get("source_note", "")})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=500)

st.divider()
st.caption(
    "Model limitations: private-credit data is opaque; SPV/off-balance-sheet "
    "exposures are incomplete; AI-specific capex is often not separately "
    "disclosed; many values are scenario assumptions, not facts. This is a "
    "stress-testing tool, not a forecasting oracle."
)
