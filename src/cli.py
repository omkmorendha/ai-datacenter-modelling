"""Command-line entry point for the AI data-center macro stress model.

Headless runner for scenarios + breakpoints, useful for example output, CI, and
notebook-free exploration. The full interactive experience is the Streamlit
dashboard (`uv run streamlit run src/app/streamlit_app.py`).

Usage:
    uv run ai-dc-model run --scenario combined_severe_case
    uv run ai-dc-model run --all
    uv run ai-dc-model breakpoints
    uv run ai-dc-model assumptions --low-confidence
    uv run ai-dc-model example > example_output.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config
from .model.datacenter_project import build_representative_project
from .model.scenarios import load_all_scenarios, load_scenario
from .model.stress_metrics import (
    compute_breakpoints,
    refinancing_gap,
    run_scenario,
    sensitivity_table,
)
from .utils.logging import get_logger

logger = get_logger("ai_dc_model.cli")


# --------------------------------------------------------------------------- #
# Optional-module wiring (everything degrades gracefully if a module is absent)
# --------------------------------------------------------------------------- #
def _try_build_modules(base_rates: dict | None = None) -> dict[str, Any]:
    """Best-effort construct the optional modules; missing ones are skipped."""
    mods: dict[str, Any] = {}

    try:
        from .model.treasury_module import build_treasury_module

        mods["treasury"] = build_treasury_module(base_rates=base_rates)
    except Exception as exc:  # pragma: no cover
        logger.info("treasury module unavailable: %s", exc)

    try:
        from .model.private_credit_module import build_private_credit_book

        mods["private_credit"] = build_private_credit_book()
    except Exception as exc:  # pragma: no cover
        logger.info("private_credit module unavailable: %s", exc)

    try:
        from .model.hyperscaler_module import build_hyperscaler_module

        mods["hyperscaler"] = build_hyperscaler_module()
    except Exception as exc:  # pragma: no cover
        logger.info("hyperscaler module unavailable: %s", exc)

    try:
        from .model.power_module import build_power_module

        mods["power"] = build_power_module()
    except Exception as exc:  # pragma: no cover
        logger.info("power module unavailable: %s", exc)

    try:
        from .model.contagion_module import build_contagion_model

        mods["contagion"] = build_contagion_model()
    except Exception as exc:  # pragma: no cover
        logger.info("contagion module unavailable: %s", exc)

    return mods


def _fmt_money(x: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def _fmt_ratio(x: float) -> str:
    if x == float("inf"):
        return "inf"
    return f"{x:.2f}"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    project = build_representative_project(capacity_mw=args.capacity_mw)
    mods = _try_build_modules()

    if args.all:
        scenarios = list(load_all_scenarios().values())
    else:
        scenarios = [load_scenario(args.scenario)]

    out: dict[str, Any] = {}
    for sc in scenarios:
        res = run_scenario(sc, project=project, **mods)
        out[sc.name] = res
        if not args.json:
            pm = res["project"]
            fs = res["financing_stack"]
            print(f"\n=== {sc.name} — {sc.label} ===")
            print(f"  {sc.description}")
            print(f"  Total debt:        {_fmt_money(pm['total_debt'])}")
            print(f"  WACD:              {fs['wacd'] * 100:.2f}%")
            print(f"  Floating (net):    {fs['floating_share_net'] * 100:.1f}%  "
                  f"(gross {fs['floating_share_gross'] * 100:.1f}%)")
            print(f"  Revenue:           {_fmt_money(pm['revenue'])}")
            print(f"  EBITDA:            {_fmt_money(pm['ebitda'])}  "
                  f"(margin {pm['ebitda_margin'] * 100:.1f}%)")
            print(f"  Interest expense:  {_fmt_money(pm['interest_expense'])}")
            print(f"  DSCR:              {_fmt_ratio(pm['dscr'])}   ICR: {_fmt_ratio(pm['icr'])}")
            print(f"  FCF after capex:   {_fmt_money(pm['fcf_after_capex'])}")
            danger = [k for k, v in pm["flags"].items() if v]
            print(f"  Danger flags:      {danger or 'none'}")
            if "private_credit" in res:
                pc = res["private_credit"]
                el = pc.get("expected_loss")
                mtm = pc.get("mark_to_market_loss")
                if el is not None:
                    print(f"  PC expected loss:  {_fmt_money(el)}   "
                          f"MTM loss: {_fmt_money(mtm) if mtm is not None else 'n/a'}")
            if "contagion" in res:
                cg = res["contagion"]
                print(f"  Funding contraction: {_fmt_money(cg['funding_contraction'])}")
                print(f"  Capex slowdown est:  {cg['capex_slowdown'] * 100:.1f}%")

    if args.json:
        print(json.dumps(_jsonable(out), indent=2, default=str))
    return 0


def cmd_breakpoints(args: argparse.Namespace) -> int:
    project = build_representative_project(capacity_mw=args.capacity_mw)
    bp = compute_breakpoints(project)
    if args.json:
        print(json.dumps(bp, indent=2, default=str))
        return 0
    print("\n=== Breakpoint panel (one-axis sensitivity from base case) ===")
    labels = {
        "max_sofr_shock_bps_before_dscr_breach": "Max SOFR shock before DSCR breach",
        "max_10y_shock_bps_before_refi_uneconomic": "Max 10Y shock before refi uneconomic",
        "max_power_price_pct_before_margin_breach": "Max power-price shock before margin breach",
        "min_utilization_before_covenant_breach": "Min utilization before covenant breach",
        "max_credit_spread_bps_before_fcf_negative": "Max credit-spread shock before FCF<0",
    }
    for key, label in labels.items():
        v = bp[key]
        if v is None:
            disp = "no breach within tested range"
        elif "pct" in key or "utilization" in key:
            disp = f"{v * 100:.1f}%"
        else:
            disp = f"+{v:.0f} bps"
        print(f"  {label:48s}: {disp}")
    print(f"\n  thresholds: {bp['thresholds_used']}")
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    rows = sensitivity_table(axis=args.axis)
    print(f"\n=== Sensitivity: {args.axis} ===")
    for r in rows:
        print(f"  {args.axis}={r[args.axis]:>7}  "
              f"DSCR={_fmt_ratio(r['dscr'])}  ICR={_fmt_ratio(r['icr'])}  "
              f"margin={r['ebitda_margin'] * 100:.1f}%  "
              f"FCF={_fmt_money(r['fcf_after_capex'])}")
    return 0


def cmd_assumptions(args: argparse.Namespace) -> int:
    try:
        from .data_sources.manual_inputs import list_assumptions, low_confidence_keys
    except Exception:
        # fallback: read raw blocks
        ba = config.load_base_assumptions()
        keys = (
            [k for k, v in ba.items() if isinstance(v, dict) and v.get("confidence") == "low"]
            if args.low_confidence else list(ba)
        )
        for k in keys:
            print(f"  {k}")
        return 0

    if args.low_confidence:
        print("\n=== LOW-CONFIDENCE assumptions (treat as scenario inputs, not facts) ===")
        for k in low_confidence_keys():
            print(f"  {k}")
        return 0

    df = list_assumptions()
    print(df.to_string(index=False))
    return 0


def cmd_example(args: argparse.Namespace) -> int:
    """Print a self-contained example output using default assumptions."""
    print("=" * 78)
    print("AI DATA-CENTER MACRO STRESS MODEL — EXAMPLE OUTPUT (default assumptions)")
    print("=" * 78)
    print("\nThis is a STRESS-TESTING tool, not a forecast. Many inputs are")
    print("low-confidence scenario assumptions, not facts.\n")

    cmd_run(argparse.Namespace(all=True, scenario=None, capacity_mw=args.capacity_mw, json=False))
    cmd_breakpoints(argparse.Namespace(capacity_mw=args.capacity_mw, json=False))

    print("\n=== Refinancing gap under combined-severe (principal at risk by year) ===")
    gap = refinancing_gap(scenario=load_scenario("combined_severe_case"))
    for year, g in gap.items():
        print(f"  {year}: maturing {_fmt_money(g['maturing_principal'])}  "
              f"refi_prob={g['refi_probability'] * 100:.0f}%  "
              f"at_risk={_fmt_money(g['principal_at_risk'])}")
    return 0


# --------------------------------------------------------------------------- #
def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and obj == float("inf"):
        return "inf"
    return obj


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-dc-model", description=__doc__)
    p.add_argument("--capacity-mw", type=float, default=100.0,
                   help="Representative project capacity in MW (default 100)")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="Run one or all scenarios")
    pr.add_argument("--scenario", default="base_case")
    pr.add_argument("--all", action="store_true", help="Run all scenarios")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_run)

    pb = sub.add_parser("breakpoints", help="Compute the breakpoint panel")
    pb.add_argument("--json", action="store_true")
    pb.set_defaults(func=cmd_breakpoints)

    ps = sub.add_parser("sensitivity", help="One-axis sensitivity table")
    ps.add_argument("--axis", default="sofr_bps")
    ps.set_defaults(func=cmd_sensitivity)

    pa = sub.add_parser("assumptions", help="List assumptions / provenance")
    pa.add_argument("--low-confidence", action="store_true")
    pa.set_defaults(func=cmd_assumptions)

    pe = sub.add_parser("example", help="Print a full example output")
    pe.set_defaults(func=cmd_example)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # --capacity-mw is a top-level arg; subparsers inherit via the namespace.
    if not hasattr(args, "capacity_mw"):
        args.capacity_mw = 100.0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
