from src.model.scenarios import load_all_scenarios, load_scenario
from src.model.datacenter_project import build_representative_project

scenarios = load_all_scenarios()
proj = build_representative_project(capacity_mw=100.0)
base = load_scenario("base_case")
m = proj.metrics(base)
print("BASE: total_debt=%.0f revenue=%.0f ebitda=%.0f margin=%.3f dscr=%.2f icr=%.2f fcf=%.0f"
      % (m["total_debt"], m["revenue"], m["ebitda"], m["ebitda_margin"], m["dscr"], m["icr"], m["fcf_after_capex"]))
print("BE: util=%.3f power=%.1f fund=%.4f" % (m["break_even_utilization"], m["break_even_power_price"], m["break_even_funding_cost"]))
print("flags:", m["flags"])
print("--- scenario sweep (dscr / icr / margin / wacd) ---")
for name, sc in scenarios.items():
    pm = proj.metrics(sc)
    wacd = proj.financing_stack.weighted_average_cost_of_debt(sc)
    print("  %-38s dscr=%.2f icr=%.2f margin=%.3f wacd=%.4f" % (name, pm["dscr"], pm["icr"], pm["ebitda_margin"], wacd))
