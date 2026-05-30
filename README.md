# AI Data-Center Macro Stress Model (v0)

A modular, scenario-based macro-financial **stress-testing tool** for the AI
data-center buildout and its interaction with U.S. Treasuries, SOFR, private
credit, securitization, hyperscaler capex, power markets, and private-market
liquidity.

This is a **working Python repo, not a narrative report**. It lets you run
scenarios and see how rate, spread, power-price, and AI-demand shocks transmit
through a mixed debt-financing stack into DSCR/ICR/FCF, refinancing risk, and
sector-level contagion.

> ⚠️ **This is a stress-testing tool, not a forecasting oracle.** Many inputs are
> low-confidence scenario assumptions, not facts. See
> [Model limitations](#model-limitations).

---

## Thesis under test

> The AI data-center buildout is shifting from internally funded hyperscaler
> capex toward a complex **debt-financed infrastructure cycle** involving private
> credit, securitization, corporate bonds, bank credit facilities,
> off-balance-sheet SPVs, project finance, and lease obligations. This is
> happening alongside heavy U.S. Treasury issuance, elevated fiscal deficits,
> geopolitical inflation risk, and fragile private-market liquidity. If Treasury
> yields, SOFR, term premium, credit spreads, or power prices rise, the AI
> infrastructure financing stack could reprice upward — stressing floating-rate
> private credit, data-center securitizations, hyperscaler free cash flow, BDCs,
> insurers, pensions, banks, and PE liquidity.

The model is built to **test, not assert**, this thesis. Two design decisions
keep it honest:

1. **It is NOT "all floating-rate private credit."** The financing stack is
   explicitly *mixed*: hyperscaler retained earnings, corporate bonds, bank
   facilities, syndicated loans, private credit, ABS/CMBS, project finance/SPVs,
   leases, and equity/JV — each with its own rate reference, fixed/floating flag,
   hedge ratio, maturity, and lender type.
2. **It is NOT "2008 subprime."** It models a possible **private-markets
   liquidity and duration-mismatch problem** with opacity, delayed marks,
   refinancing risk, and Treasury-led repricing — distinct from bank insolvency.

---

## What you can do with it

Run scenarios and inspect:

1. How **Treasury-yield shocks** raise AI/DC funding costs.
2. How **SOFR shocks** raise floating-rate debt service (net of hedges/swaps).
3. How **credit-spread shocks** make refinancing more expensive.
4. How **power-price shocks** compress data-center operating margins.
5. How **AI revenue/utilization disappointments** erode debt-service capacity.
6. How stress **transmits** into private credit, BDCs, insurers, pensions, banks,
   and PE liquidity (a stylized contagion network).
7. Where the **breakpoints** are — the shock level at which DSCR, ICR,
   FCF-after-capex, or refinancing turn dangerous.

---

## Quick start (uses a `uv`-managed venv)

```bash
# 1. Create the virtual environment (.venv) with Python 3.11
uv venv --python 3.11

# 2. Install the package (editable) + dev tools into the venv
uv pip install -e ".[dev]"

# 3. Run the tests
uv run pytest

# 4. Launch the dashboard
uv run streamlit run src/app/streamlit_app.py

# 5. Or use the CLI
uv run ai-dc-model example          # full example output (default assumptions)
uv run ai-dc-model run --all        # every scenario
uv run ai-dc-model run --scenario combined_severe_case
uv run ai-dc-model breakpoints      # the breakpoint panel
uv run ai-dc-model sensitivity --axis sofr_bps
uv run ai-dc-model assumptions --low-confidence
```

**No API keys are required for v0.** Everything runs offline from the YAML
assumption layer and cached data. Optional keys (FRED, EIA) and a SEC User-Agent
enrich the model with live data — copy `.env.example` to `.env` to add them.
Set `AI_DC_OFFLINE=1` to force offline mode.

---

## Model structure

```
ai-datacenter-macro-model/
  README.md                       this file
  pyproject.toml                  uv/pip project; deps; ruff/black/pytest config
  .env.example                    optional API keys (all optional for v0)
  example_output.txt              sample CLI output with default assumptions
  data/
    assumptions/
      base_assumptions.yaml       structured, sourced assumption blocks
      scenarios.yaml              the 7 stress scenarios
      entity_universe.yaml        hyperscalers/REITs + CIKs + XBRL tag map
    raw/cache/                    on-disk HTTP cache (gitignored)
  src/
    config.py                     paths, env keys, cached YAML loaders
    cli.py                        command-line runner
    data_sources/                 cached, offline-safe API clients
      fred.py                     SOFR / Treasury yields (FRED)
      fiscaldata_treasury.py      Treasury auctions / supply (FiscalData)
      sec_edgar.py                10-K/10-Q XBRL companyfacts
      eia.py                      electricity / power prices
      manual_inputs.py            surfaces YAML assumptions + provenance
    model/
      scenarios.py                typed Scenario shock vocabulary
      financing_stack.py          mixed capital stack: WACD, net floating, walls
      datacenter_project.py       revenue/EBITDA/DSCR/ICR/FCF/break-evens
      treasury_module.py          Treasury Stress Index + rate/refi/cap shocks
      power_module.py             regional power cost, margin & capex-delay
      hyperscaler_module.py       public-co financials + AI-capex pressure
      private_credit_module.py    sector EL/MTM/investor allocation (low-conf)
      contagion_module.py         stylized exposure-matrix transmission
      stress_metrics.py           integration: run_scenario, breakpoints, sens.
    app/streamlit_app.py          interactive dashboard (8 tabs)
    utils/
      logging.py                  structured data-quality log events
      validation.py               safe_div, zscore, bps/pct helpers
  notebooks/
    01_data_exploration.ipynb     pull/inspect data + assumptions
    02_scenario_walkthrough.ipynb run scenarios + breakpoints end-to-end
  tests/                          pytest suite (financing, DSCR/ICR, treasury, PC)
```

### How the modules compose

```
                        scenarios.yaml ──► Scenario (typed shock bundle)
                                              │
   base_assumptions.yaml ──► config ──► build_representative_project()
                                              │
   FRED/EIA/EDGAR/FiscalData (optional, cached) ──► base_rates / financials
                                              │
              ┌───────────────┬──────────────┼───────────────┬──────────────┐
        treasury_module   financing_stack  datacenter_project  power_module  hyperscaler_module
              │               │              │                  │              │
              └───────────────┴──────────────┴──────────────────┴──────────────┘
                                              │
                              private_credit_module  ──►  contagion_module
                                              │
                                       stress_metrics
                            (run_scenario · breakpoints · sensitivity · refi gap)
                                              │
                                  CLI  /  Streamlit dashboard
```

Every cross-module dependency is **optional and duck-typed**: if a module or a
remote data source is unavailable, the rest of the model still runs and the
panel shows a clear notice.

---

## Data sources

All clients **cache to disk**, **respect offline mode**, and **degrade
gracefully** (log a warning, return `None`/empty — never crash the model).

| Source | Client | Used for | Key? |
|---|---|---|---|
| **FRED** (St. Louis Fed) | `data_sources/fred.py` | SOFR, EFFR, 2Y/10Y/30Y (`DGS2/10/30`), inflation exp (`T10YIE`) | Optional (falls back to public CSV) |
| **U.S. Treasury FiscalData** | `data_sources/fiscaldata_treasury.py` | Auction bid-to-cover / weak-demand proxy, debt/issuance | No key |
| **SEC EDGAR** | `data_sources/sec_edgar.py` | 10-K/10-Q XBRL: capex, OCF, debt, leases, revenue, interest | No key (needs descriptive User-Agent) |
| **EIA** | `data_sources/eia.py` | Retail/wholesale electricity prices, gas | Optional (else YAML power assumptions) |
| **Manual / YAML** | `data_sources/manual_inputs.py` | Private credit, SPV, AI-capex share, power, DC unit economics | n/a — the assumption layer |

Calibration framing (used to set assumptions, not machine-ingested): **BIS**
(private-credit/AI-investment macro), **IMF** (fiscal/GFSR/bond-market risk),
**Federal Reserve / FOMC FSR** (rates, leverage, private credit, bank exposure),
**ECB FSR** (private-credit vulnerability, insurer/pension channels), **FSB**
(private-credit systemic-risk channels, opacity, interlinkages).

---

## The seven scenarios

Defined in `data/assumptions/scenarios.yaml`. Each is a bundle of shocks relative
to the base case (`*_bps` additive basis points, `*_pct` multiplicative,
`*_mult` multipliers, `*_delta` additive native units).

| Scenario | Headline shocks |
|---|---|
| `base_case` | No shocks; securitization open; high AI utilization |
| `high_for_longer` | SOFR +100, 10Y/30Y +75, spreads +75, power +10% |
| `treasury_term_premium_shock` | SOFR +50, 10Y +150, 30Y +200, spreads +150, securitization haircut up |
| `iran_oil_inflation_shock` | SOFR +100, 10Y +100, 30Y +125, **power +25%**, infl exp +50, spreads +100 |
| `ai_revenue_disappointment` | **Utilization −20%, revenue/MW −15%**, spreads +100, equity −25%, capex cuts |
| `severe_private_credit_liquidity_crunch` | SOFR +150, spreads +250, **PC NAV −15%, PD ×2**, recovery down, new lending −40%, securitization closed |
| `combined_severe_case` | Treasury shock + AI disappointment + power shock + PC crunch, stacked |

### Example output (default assumptions, representative 100 MW project)

The base case is deliberately a **healthy, financeable project** so that stress
comes from the shocks, not a broken starting point:

| Scenario | DSCR | ICR | EBITDA margin | WACD |
|---|---|---|---|---|
| base_case | 2.75 | 2.97 | 61.6% | 8.55% |
| high_for_longer | 2.45 | 2.63 | 58.5% | 9.16% |
| treasury_term_premium_shock | 2.53 | 2.72 | 61.6% | 9.34% |
| iran_oil_inflation_shock | 2.23 | 2.40 | 53.9% | 9.27% |
| ai_revenue_disappointment | 1.44 | 1.56 | 52.2% | 8.98% |
| severe_private_credit_liquidity_crunch | 2.36 | 2.53 | 61.6% | 10.06% |
| **combined_severe_case** | **1.05** | **1.12** | **43.1%** | **10.27%** |

The story the model tells: a single well-structured project (power-inclusive
lease, ~55% leverage) is robust to **any one** shock axis (breakpoints of
+4123 bps SOFR before a DSCR breach, +3439 bps spread before FCF turns negative;
a 10Y move doesn't breach within the tested range because existing fixed coupons
don't reprice live — it bites through the refinancing channel instead). The
danger emerges from the **combination** (combined-severe pushes DSCR to 1.05,
margin to the covenant edge) and from **sector-wide** private-credit and
contagion channels — not from one isolated knob. See `example_output.txt` for the
full run.

---

## How to add / change assumptions

Every manually estimated value in `data/assumptions/base_assumptions.yaml` is a
**structured block** with full provenance:

```yaml
private_credit_ai_datacenter_outstanding:
  value: 250_000_000_000
  unit: USD
  confidence: low            # low | medium | high
  source_note: "Manual midpoint based on public BIS/market estimates; replace with better data when available."
  last_updated: "2026-05-30"
  rationale: "Private-credit exposure is opaque; use scenario sensitivity rather than point estimate."
```

To change the model's behavior:

- **Edit a base assumption** → change the `value:` in `base_assumptions.yaml`.
  The factories (`build_representative_project`, `build_private_credit_book`, …)
  read these via `config.assumption_value(key)`.
- **Add a scenario** → add an entry under `scenarios:` in `scenarios.yaml`.
- **Add a company** → add it to `entity_universe.yaml` with its 10-digit CIK and
  an `ai_capex_share_override` (AI-specific capex is rarely disclosed, so this is
  a manual override, not a fact).
- **Live overrides in the dashboard** → the Assumptions tab and the sidebar
  sliders override values for the session without writing the YAML.

Inspect provenance from code:

```python
from src.data_sources.manual_inputs import list_assumptions, low_confidence_keys
list_assumptions()        # DataFrame: key, value, unit, confidence, source_note, ...
low_confidence_keys()     # the values you should treat as scenario inputs, not facts
```

---

## Dashboard

`uv run streamlit run src/app/streamlit_app.py` — eight tabs:

1. **Project & Coverage** — economics + DSCR/ICR by scenario
2. **Financing stack** — mixed capital stack table, floating share (gross vs
   net), exposure by lender type, composition pie
3. **Treasury / SOFR** — yield curve (base vs shocked), Treasury Stress Index,
   securitization openness
4. **Refinancing wall** — maturity wall vs scenario-adjusted principal-at-risk
5. **Sensitivity** — one-axis sweep table + DSCR/ICR chart
6. **Breakpoints** — max SOFR / 10Y / power / spread and min utilization
7. **Private credit & contagion** — EL vs MTM (distinct!), investor loss
   allocation, funding contraction, capex slowdown, systemic-risk heatmap
8. **Assumptions** — editable provenance table filtered by confidence

The sidebar has a scenario selector plus live shock-override sliders.

---

## Key modeling distinctions (deliberately enforced)

The spec calls these out and the code honors them:

- **Floating exposure vs *net* floating** — after hedges and swaps. Only net
  floating reprices with SOFR.
- **Hyperscaler debt vs operator/SPV/project debt** — tracked in separate
  modules.
- **Committed vs drawn** — `amount` is drawn debt; new-lending-capacity covers
  the committed/pipeline side.
- **Capex announced vs spent** — AI-capex share is a manual override, not a
  disclosed figure.
- **DC power demand vs total commercial electricity demand** — noted in the power
  module.
- **Mark-to-market stress vs realized default loss** — `mark_to_market_loss`
  (NAV haircut) and `expected_loss` (PD × LGD) are computed separately and must
  **not** be summed.
- **PE liquidity crunch vs 2008-style bank insolvency** — the contagion model is
  a stylized liquidity/repricing cascade, not an insolvency model.

---

## Logging

The model is loud about data quality. Structured, greppable events
(`src/utils/logging.py`): `MISSING_DATA`, `STALE_DATA`, `ASSUMPTION_USED`,
`API_FAILURE`, `SCENARIO_RESULT`. Set `AI_DC_LOG_LEVEL=DEBUG` for more.

---

## Testing

```bash
uv run pytest
```

Covers: weighted average cost of debt; fixed vs floating debt service; hedge/swap
treatment (net floating); DSCR & ICR; scenario application (monotonic stress);
refinancing wall; break-even/breakpoints; Treasury Stress Index (base ≈ 0,
severe > mild, missing components dropped); private-credit expected loss, MTM,
net floating, and investor loss allocation.

Tests for not-yet-built modules use `pytest.importorskip`, so the suite stays
green as the model grows.

---

## Model limitations

This is a **stress-testing tool, not a forecasting oracle.** In particular:

- **Private-credit data is opaque.** Outstanding, floating share, hedge ratios,
  LTV, DSCR, PD, and recovery are **low-confidence estimates**, calibrated to
  public BIS/IMF/FSB/ECB framing — not observed data. Use the *sensitivity*, not
  the point estimates.
- **SPV / off-balance-sheet exposures are incomplete.** Project-finance and
  special-purpose-vehicle structures are not fully disclosed; the financing stack
  is a representative illustration.
- **AI-specific capex is often not separately disclosed.** Hyperscalers report
  total capex; the AI/data-center share is a manual override per company.
- **Many values are scenario assumptions, not facts.** The whole point is to vary
  them and see what breaks.
- **The contagion model is stylized.** It is a linear exposure-matrix cascade
  with illustrative weights — a way to reason about transmission channels, not a
  calibrated systemic-risk forecast.
- **Breakpoints are one-dimensional.** Each holds everything else at base and
  moves one axis; read them as per-axis headroom, not joint probabilities.

When a number looks precise, remember it usually inherits a low-confidence
assumption. Treat the model as a way to ask *"what would have to be true for this
to break, and how far are we from that?"* — not as a prediction.

---

## License

MIT.
